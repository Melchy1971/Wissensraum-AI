import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from contextlib import contextmanager
from unittest.mock import Mock
from unittest.mock import patch
import sys
from contextlib import contextmanager
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(Path(__file__).resolve().parent))  # test_settings, also for `-m unittest tests.test_database`

from core import PostgresStore, _PostgresConnection
from configuration_authority import configuration_operation_lock
from database import DatabaseSettings, resolve_sqlite_path
from migrations import CURRENT_SCHEMA_VERSION
from test_settings import FakeVault


class FakeCursor:
    def __init__(self): self.query=None
    def __enter__(self): return self
    def __exit__(self,*_): pass
    def execute(self,query): self.query=query
    def fetchone(self): return ('wissen_test','test_user','PostgreSQL 18.4, synthetic')


class FakeConnection:
    def __init__(self): self.closed=False; self.cursor_value=FakeCursor()
    def cursor(self): return self.cursor_value
    def close(self): self.closed=True


class FakeResetConnection:
    def __init__(self, tuple_rows=False):
        self.closed=False
        self.calls=[]
        self.schema='old'
        self.rolled_back=False
        self.tuple_rows=tuple_rows

    @contextmanager
    def transaction(self):
        before=self.schema
        try:
            yield
        except Exception:
            self.schema=before
            self.rolled_back=True
            raise

    def execute(self,query,parameters=()):
        self.calls.append((query,parameters))
        if query.startswith('DROP SCHEMA'):
            self.schema=None
        if 'FROM information_schema.tables' in query:
            tables=['accounts','ai_runs','ai_usage','audit_log','chunk_vectors','chunks','collection_documents','collections','document_relations','document_tags','document_versions','documents','email_accounts',
                    'email_attachments','email_imports','entities','entity_mentions','entity_types','favorites','grants','graph_changes',
                    'graph_relations','import_item_errors','import_items','import_jobs','login_requests',
                    'metadata_changes','organizations','relation_types','rule_run_changes','rule_runs','rules','saved_searches','schema_migrations','settings','sources',
                    'tags','web_sessions'] if self.schema == 'new' else []
            rows=[(name,) for name in tables] if self.tuple_rows else [{'table_name':name} for name in tables]
            return type('Result',(),{'fetchall':lambda _:rows})()
        if 'SELECT MAX(version)' in query:
            version=CURRENT_SCHEMA_VERSION if self.schema == 'new' else None
            row=(version,) if self.tuple_rows else {'version':version}
            return type('Result',(),{'fetchone':lambda _:row})()
        return type('Result',(),{'fetchall':lambda _:[]})()

    def close(self): self.closed=True


class DatabaseSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.base=Path(self.temp.name)
        self.vault=FakeVault()
        self.settings=DatabaseSettings(self.base,self.vault,environ={})
        self.values={'host':'db.example.test','port':'5432','database':'wissen_test',
                     'username':'test_user','sslmode':'require'}
    def tearDown(self): self.temp.cleanup()

    @contextmanager
    def _configuration_operation_owned_by_process(self):
        ready=self.base/'database-lock-ready'
        release=self.base/'database-lock-release'
        script=(
            "import pathlib,time; from configuration_authority import configuration_operation_lock; "
            "lock=configuration_operation_lock(pathlib.Path(r'"+str(self.base)+"')); "
            "lock.acquire(); pathlib.Path(r'"+str(ready)+"').write_text('ready'); "
            "\nwhile not pathlib.Path(r'"+str(release)+"').exists(): time.sleep(.01)\n"
            "lock.release()")
        process=subprocess.Popen([sys.executable,'-c',script],
                                 cwd=str(Path(__file__).parents[1]))
        try:
            deadline=time.monotonic()+5
            while not ready.exists() and process.poll() is None and time.monotonic()<deadline:
                time.sleep(.01)
            self.assertTrue(ready.exists())
            yield
        finally:
            release.write_text('release',encoding='utf-8')
            try:
                returncode=process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                self.fail('lock-owner process did not exit after release request')
            self.assertEqual(returncode,0,'lock-owner process exited unexpectedly')

    def test_sqlite_path_precedence_is_explicit_then_environment_then_default(self):
        environment={'WISSEN_DB':'environment.db'}
        before=dict(environment)

        self.assertEqual(resolve_sqlite_path(self.base,{}),self.base/'data'/'wissen.db')
        self.assertEqual(resolve_sqlite_path(self.base,environment),Path('environment.db'))
        self.assertEqual(resolve_sqlite_path(self.base,environment,Path('explicit.db')),
                         Path('explicit.db'))
        self.assertEqual(environment,before)

    def test_profile_is_saved_without_password(self):
        self.settings.save(self.values,'synthetic-db-password')
        saved=self.settings.path.read_text(encoding='utf-8')
        self.assertNotIn('synthetic-db-password',saved)
        self.assertEqual(self.vault.get('database:postgresql'),'synthetic-db-password')
        self.assertEqual(json.loads(saved)['host'],'db.example.test')
        self.assertEqual(self.settings.profile()['backend'],'sqlite')

    def test_profile_write_failure_restores_prior_vault_value(self):
        original_profile={**self.values,'backend':'sqlite','port':5432}
        self.settings._write(original_profile)
        before=self.settings.path.read_bytes()
        self.vault.set(self.settings.secret_name,'original-password')
        failure=OSError('C:/private/database.json: replacement-secret')
        with patch('database.os.replace',side_effect=failure):
            with self.assertRaisesRegex(
                    ValueError,
                    '^Datenbankkonfiguration konnte nicht gespeichert werden\\.$') as caught:
                self.settings.save(self.values,'replacement-secret')
        self.assertEqual(self.vault.get(self.settings.secret_name),'original-password')
        self.assertEqual(self.settings.path.read_bytes(),before)
        self.assertNotIn('replacement-secret',str(caught.exception))
        self.assertNotIn('private',str(caught.exception))

    def test_profile_write_failure_restores_prior_vault_absence(self):
        with patch('database.os.replace',side_effect=OSError('write failed')):
            with self.assertRaisesRegex(
                    ValueError,
                    '^Datenbankkonfiguration konnte nicht gespeichert werden\\.$'):
                self.settings.save(self.values,'replacement-secret')
        self.assertIsNone(self.vault.get(self.settings.secret_name))

    def test_vault_rollback_failure_is_distinct_fixed_and_redacted(self):
        self.vault.set(self.settings.secret_name,'original-password')
        real_set=self.vault.set
        def set_password(name,password):
            if password=='original-password':
                raise OSError('rollback leaked original-password at C:/vault/private')
            real_set(name,password)
        with patch.object(self.vault,'set',side_effect=set_password), \
             patch('database.os.replace',
                   side_effect=OSError('profile leaked replacement-secret at C:/profile/private')):
            with self.assertRaisesRegex(
                    ValueError,
                    '^Datenbankkonfiguration konnte nicht sicher wiederhergestellt werden; Zustand prüfen\\.$') as caught:
                self.settings.save(self.values,'replacement-secret')
        message=str(caught.exception)
        self.assertNotIn('original-password',message)
        self.assertNotIn('replacement-secret',message)
        self.assertNotIn('private',message)

    def test_baseexception_during_profile_write_restores_vault_before_propagating(self):
        self.vault.set(self.settings.secret_name,'original-password')
        interruption=KeyboardInterrupt('replacement-secret C:/profile/private')
        with patch('database.os.replace',side_effect=interruption):
            with self.assertRaises(KeyboardInterrupt) as caught:
                self.settings.save(self.values,'replacement-secret')
        self.assertIs(caught.exception,interruption)
        self.assertEqual(self.vault.get(self.settings.secret_name),'original-password')

    def test_profile_validation_rejects_hostile_scalar_subclasses_without_hooks(self):
        class HostileString(str):
            called=False
            def hook(self,*_args,**_kwargs):
                type(self).called=True
                raise AssertionError('hostile profile scalar hook called')
            __eq__=__ne__=__hash__=__str__=__iter__=hook
            lower=strip=isdigit=hook
        class HostileInteger(int):
            called=False
            def hook(self,*_args,**_kwargs):
                type(self).called=True
                raise AssertionError('hostile profile integer hook called')
            __eq__=__ne__=__hash__=__int__=__index__=hook
            __lt__=__le__=__gt__=__ge__=hook

        values = (
            {'backend':HostileString('sqlite')},
            {'backend':'postgresql','host':HostileString('db.example.test')},
            {'backend':'postgresql','port':HostileInteger(5432)},
        )
        for value in values:
            with self.subTest(value=value):
                self.settings.path.parent.mkdir(parents=True,exist_ok=True)
                self.settings.path.write_text('{}',encoding='utf-8')
                with patch('database.json.loads',return_value=value):
                    with self.assertRaises(ValueError):
                        self.settings.persisted_profile()
                self.assertFalse(HostileString.called or HostileInteger.called)

    def test_profile_write_uses_independent_unique_temporaries(self):
        first=DatabaseSettings(self.base,self.vault,environ={})
        second=DatabaseSettings(self.base,self.vault,environ={})
        profiles=(
            {'backend':'sqlite','host':'','port':5432,'database':'','username':'','sslmode':'require'},
            {'backend':'postgresql','host':'db.example.test','port':5432,
             'database':'wissen','username':'user','sslmode':'require'},
        )
        sources=[]
        real_replace=os.replace
        def replace(source,target):
            sources.append(Path(source))
            return real_replace(source,target)
        with patch('database.os.replace',side_effect=replace):
            first._write(profiles[0])
            second._write(profiles[1])
        self.assertEqual(len(set(sources)),2)
        self.assertNotIn(self.settings.path.with_suffix('.tmp'),sources)
        self.assertIn(json.loads(self.settings.path.read_text(encoding='utf-8')),profiles)
        self.assertFalse(any(self.settings.path.parent.glob('database.json.tmp-*')))

    def test_profile_write_syncs_file_then_replaces_then_syncs_directory(self):
        operations=[]
        real_replace=os.replace
        with patch('database.os.fsync',side_effect=lambda _fd:operations.append('file')), \
             patch('database.os.replace',side_effect=lambda source,target:
                   (operations.append('replace'),real_replace(source,target))[1]), \
             patch('database._sync_directory',side_effect=lambda _path:operations.append('directory')):
            self.settings._write({'backend':'sqlite'})
        self.assertEqual(operations[-3:],['file','replace','directory'])

    def test_profile_write_cleans_unique_temporary_after_exception_or_baseexception(self):
        temporary=self.settings.path.with_name(self.settings.path.name+'.tmp-'+'1'*32)
        fixed_uuid=type('FixedUuid',(),{'hex':'1'*32})()
        for failure in (OSError('replace failed'),KeyboardInterrupt('interrupted')):
            with self.subTest(failure=type(failure).__name__):
                self.settings.path.parent.mkdir(parents=True,exist_ok=True)
                self.settings.path.write_bytes(b'previous')
                with patch('database.uuid.uuid4',return_value=fixed_uuid), \
                     patch('database.os.replace',side_effect=failure):
                    with self.assertRaises(type(failure)) as caught:
                        self.settings._write({'backend':'sqlite'})
                self.assertIs(caught.exception,failure)
                self.assertFalse(temporary.exists())
                self.assertEqual(self.settings.path.read_bytes(),b'previous')

    def test_deeply_nested_profile_is_rejected_with_fixed_message(self):
        self.settings.path.parent.mkdir(parents=True,exist_ok=True)
        data=b'['*100000+b']'*100000
        self.settings.path.write_bytes(data)
        for read in (self.settings.profile,self.settings.persisted_profile):
            with self.subTest(read.__name__):
                with self.assertRaisesRegex(ValueError,'^Datenbankkonfiguration ist nicht lesbar'):
                    read()
        self.assertEqual(self.settings.path.read_bytes(),data)

    def test_interrupted_profile_write_before_replace_keeps_previous_profile(self):
        self.settings.path.parent.mkdir(parents=True,exist_ok=True)
        self.settings.path.write_bytes(b'{"backend":"sqlite"}')
        with patch('database.os.fsync',side_effect=OSError(28,'voll')):
            with self.assertRaises(OSError):
                self.settings._write({'backend':'postgresql'})
        self.assertEqual(self.settings.path.read_bytes(),b'{"backend":"sqlite"}')
        self.assertFalse(any(self.settings.path.parent.glob('database.json.tmp-*')))

    def test_profile_write_fails_closed_while_configuration_operation_is_owned(self):
        lock=configuration_operation_lock(self.base)
        lock.acquire()
        try:
            with self.assertRaises(BlockingIOError):
                DatabaseSettings(self.base,self.vault,environ={})._write({'backend':'sqlite'})
            self.assertFalse(self.settings.path.exists())
        finally:
            lock.release()
        self.settings._write({'backend':'sqlite'})
        self.assertEqual(json.loads(self.settings.path.read_text(encoding='utf-8')),
                         {'backend':'sqlite'})

    def test_save_busy_lock_preserves_vault_and_profile(self):
        original={**self.values,'backend':'sqlite','port':5432}
        self.settings._write(original)
        self.vault.set(self.settings.secret_name,'original-password')
        before=self.settings.path.read_bytes()

        with self._configuration_operation_owned_by_process():
            with self.assertRaisesRegex(
                    ValueError,
                    '^Datenbankkonfiguration wird gerade geändert; bitte erneut versuchen\\.$'):
                self.settings.save({**self.values,'database':'replacement'},'replacement-password')

        self.assertEqual(self.vault.get(self.settings.secret_name),'original-password')
        self.assertEqual(self.settings.path.read_bytes(),before)

    def test_postgresql_activation_busy_lock_skips_connection_schema_and_profile_change(self):
        self.settings._write({'backend':'sqlite'})
        before=self.settings.path.read_bytes()
        connector=Mock(side_effect=AssertionError('connection attempted'))
        store_factory=Mock(side_effect=AssertionError('schema initialization attempted'))

        with self._configuration_operation_owned_by_process():
            with self.assertRaisesRegex(
                    ValueError,
                    '^Datenbankkonfiguration wird gerade geändert; bitte erneut versuchen\\.$'):
                self.settings.activate_postgresql(
                    self.values,'synthetic-db-password',connector=connector,
                    store_factory=store_factory)

        connector.assert_not_called()
        store_factory.assert_not_called()
        self.assertEqual(self.settings.path.read_bytes(),before)
        self.assertIsNone(self.vault.get(self.settings.secret_name))

    def test_sqlite_activation_busy_lock_skips_profile_read_and_write(self):
        self.settings._write({**self.values,'backend':'postgresql','port':5432})
        before=self.settings.path.read_bytes()

        with patch.object(self.settings,'profile',wraps=self.settings.profile) as profile:
            with self._configuration_operation_owned_by_process():
                with self.assertRaisesRegex(
                        ValueError,
                        '^Datenbankkonfiguration wird gerade geändert; bitte erneut versuchen\\.$'):
                    self.settings.activate_sqlite()

        profile.assert_not_called()
        self.assertEqual(self.settings.path.read_bytes(),before)

    def test_postgresql_reset_busy_lock_skips_destructive_sql(self):
        self.settings._write({**self.values,'backend':'postgresql','port':5432})
        before=self.settings.path.read_bytes()

        with patch.object(self.settings,'_connect',
                          side_effect=AssertionError('destructive SQL connection attempted')) as connect:
            with self._configuration_operation_owned_by_process():
                with self.assertRaisesRegex(
                        ValueError,
                        '^Datenbankkonfiguration wird gerade geändert; bitte erneut versuchen\\.$'):
                    self.settings.reset_postgresql(
                        self.values,'synthetic-db-password',self.values['database'])

        connect.assert_not_called()
        self.assertEqual(self.settings.path.read_bytes(),before)

    def test_missing_profile_keeps_backward_compatible_sqlite_default(self):
        self.assertFalse(self.settings.path.exists())
        self.assertEqual(self.settings.profile()['backend'],'sqlite')

    def test_explicit_invalid_environment_backend_values_fail_closed(self):
        for value in (None, '', '   ', 'unexpected'):
            with self.subTest(value=value):
                settings=DatabaseSettings(self.base,self.vault,environ={'DB_BACKEND':value})
                with self.assertRaisesRegex(ValueError,'Datenbankkonfiguration'):
                    settings.profile()

    def test_malformed_profile_fails_closed_instead_of_selecting_sqlite(self):
        self.settings.path.parent.mkdir()
        self.settings.path.write_text('{malformed',encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'Datenbankkonfiguration'):
            self.settings.profile()

    def test_unknown_backend_fails_closed_instead_of_selecting_sqlite(self):
        self.settings.path.parent.mkdir()
        self.settings.path.write_text('{"backend":"unexpected"}',encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'Datenbankkonfiguration'):
            self.settings.profile()

    def test_explicit_empty_backend_values_fail_closed(self):
        self.settings.path.parent.mkdir()
        for value in (None, '', '   '):
            with self.subTest(value=value):
                self.settings.path.write_text(json.dumps({'backend':value}),encoding='utf-8')
                with self.assertRaisesRegex(ValueError,'Datenbankkonfiguration'):
                    self.settings.profile()

    def test_unreadable_profile_path_fails_closed(self):
        self.settings.path.mkdir(parents=True)
        with self.assertRaisesRegex(ValueError,'Datenbankkonfiguration'):
            self.settings.profile()

    def test_connection_test_uses_tls_and_redacts_password(self):
        captured={}
        connection=FakeConnection()
        def connector(**values): captured.update(values); return connection
        result=self.settings.test(self.values,'synthetic-db-password',connector)
        self.assertEqual(result['database'],'wissen_test')
        self.assertEqual(captured['sslmode'],'require')
        self.assertEqual(captured['connect_timeout'],10)
        self.assertTrue(connection.closed)
        self.assertNotIn('synthetic-db-password',str(result))

    def test_activation_initializes_schema_and_requires_restart(self):
        created=[]
        self.settings.activate_postgresql(self.values,'synthetic-db-password',
                                            connector=lambda **_:FakeConnection(),
                                            store_factory=lambda profile,password:created.append((profile,password)))
        self.assertEqual(self.settings.profile()['backend'],'postgresql')
        self.assertEqual(created[0][0]['database'],'wissen_test')
        self.assertEqual(created[0][1],'synthetic-db-password')
        self.settings.activate_sqlite()
        self.assertEqual(self.settings.profile()['backend'],'sqlite')

    def test_invalid_connection_values_are_rejected(self):
        for change in ({'host':'https://db.example.test'},{'port':'70000'},{'sslmode':'disable'}):
            with self.subTest(change=change),self.assertRaises(ValueError):
                self.settings.connection_values({**self.values,**change},'synthetic-db-password')

    def test_password_must_be_a_nonempty_exact_string(self):
        class StatefulString(str):
            pass

        for password in ('', True, 1234, StatefulString('synthetic-db-password')):
            with self.subTest(password_type=type(password)),self.assertRaisesRegex(ValueError,'Kennwort'):
                self.settings.connection_values(self.values,password)

    def test_connection_values_rejects_non_exact_environment_passwords(self):
        class StatefulString(str):
            def __str__(self):
                raise AssertionError('stateful string must not be stringified')

        for password in (1234, StatefulString('synthetic-db-password')):
            with self.subTest(password_type=type(password)):
                settings=DatabaseSettings(self.base,self.vault,environ={'DB_PASSWORD':password})
                with self.assertRaisesRegex(ValueError,'Kennwort'):
                    settings.connection_values(self.values)

    def test_public_password_paths_reject_explicit_false_zero_and_empty(self):
        self.vault.set(self.settings.secret_name, 'stored-db-password')

        operations = {
            'save': lambda password: self.settings.save(self.values, password),
            'activate_postgresql': lambda password: self.settings.activate_postgresql(
                self.values, password, connector=lambda **_:FakeConnection(),
                store_factory=lambda *_:None),
            'reset_postgresql': lambda password: self.settings.reset_postgresql(
                self.values, password, 'wrong-confirmation'),
        }
        for name,operation in operations.items():
            for password in (False, 0, ''):
                with self.subTest(operation=name, password=password):
                    with self.assertRaisesRegex(ValueError,'Kennwort'):
                        operation(password)

    def test_reset_requires_exact_database_confirmation(self):
        with self.assertRaisesRegex(ValueError,'nichts gelöscht'):
            self.settings.reset_postgresql(self.values,'synthetic-db-password','falsche_datenbank')

    def test_reset_recreates_and_validates_on_same_locked_transaction(self):
        connection=FakeResetConnection(tuple_rows=True)

        def migrate(adapter,*,acquire_lock):
            self.assertIs(adapter.connection,connection)
            self.assertFalse(acquire_lock)
            connection.schema='new'

        with patch.object(self.settings,'_connect',return_value=connection), \
             patch('migrations.migrate_postgres',side_effect=migrate) as migration:
            result=self.settings.reset_postgresql(
                self.values,'synthetic-db-password',self.values['database'])
        migration.assert_called_once()
        queries=[query for query,_ in connection.calls]
        self.assertLess(next(i for i,q in enumerate(queries) if 'pg_advisory_xact_lock' in q),
                        next(i for i,q in enumerate(queries) if q.startswith('DROP SCHEMA')))
        self.assertEqual(sum('pg_advisory_xact_lock' in q for q in queries),1)
        self.assertLess(next(i for i,q in enumerate(queries) if q.startswith('SET LOCAL lock_timeout')),
                        next(i for i,q in enumerate(queries) if 'pg_advisory_xact_lock' in q))
        self.assertEqual(result['tables'],['accounts','ai_runs','ai_usage','audit_log','chunk_vectors','chunks','collection_documents','collections',
                                           'document_relations','document_tags','document_versions','documents',
                                           'email_accounts','email_attachments','email_imports','entities',
                                           'entity_mentions','entity_types','favorites','grants','graph_changes',
                                           'graph_relations','import_item_errors','import_items','import_jobs','login_requests',
                                           'metadata_changes','organizations','relation_types','rule_run_changes',
                                           'rule_runs','rules','saved_searches','schema_migrations','settings','sources','tags','web_sessions'])
        self.assertTrue(connection.closed)

    def test_failed_reset_rolls_back_and_preserves_previous_schema(self):
        connection=FakeResetConnection()

        def fail_after_recreation(adapter,*,acquire_lock):
            connection.schema='partial-new'
            raise RuntimeError('synthetic migration failure')

        with patch.object(self.settings,'_connect',return_value=connection), \
             patch('migrations.migrate_postgres',side_effect=fail_after_recreation), \
             self.assertRaisesRegex(ValueError,'zurückgesetzt'):
            self.settings.reset_postgresql(
                self.values,'synthetic-db-password',self.values['database'])
        self.assertTrue(connection.rolled_back)
        self.assertEqual(connection.schema,'old')

    def test_postgres_adapter_translates_parameters(self):
        calls=[]
        fake=type('Connection',(),{'execute':lambda _,query,parameters:calls.append((query,parameters))})()
        _PostgresConnection(fake).execute('SELECT * FROM settings WHERE name=?',('active_provider',))
        _PostgresConnection(fake).execute('SELECT 1 WHERE name LIKE ?',('probe_%',))
        self.assertEqual(calls,[
            ('SELECT * FROM settings WHERE name=%s',('active_provider',)),
            ('SELECT 1 WHERE name LIKE %s',('probe_%',)),
        ])

    def _contract_connection(self,denied_fragment=None,sequence_rows=None):
        class FakeResult:
            def __init__(self,rows=()): self.rows=list(rows)
            def fetchall(self): return self.rows
            def fetchone(self): return self.rows[0] if self.rows else None
        class ContractConnection:
            def __init__(self):
                self.queries=[]; self.rollback_count=0; self.closed=False; self.pending=set()
            def execute(inner,query,parameters=()):
                inner.queries.append((query,parameters))
                if 'information_schema.columns' in query:
                    return FakeResult([(table,column,*contract)
                                       for table,columns in PostgresStore.column_contract.items()
                                       for column,contract in columns.items()])
                if 'pg_constraint' in query:
                    return FakeResult([(name,*contract)
                                       for name,contract in PostgresStore.required_constraints.items()])
                if 'pg_indexes' in query:
                    return FakeResult([
                        ('chunks_document_id_idx','chunks','CREATE INDEX chunks_document_id_idx ON wissensraum.chunks (document_id)'),
                        ('chunks_search_idx','chunks','CREATE INDEX chunks_search_idx ON wissensraum.chunks USING gin (search_vector)'),
                        ('documents_search_idx','documents','CREATE INDEX documents_search_idx ON wissensraum.documents USING gin (search_vector)'),
                    ])
                if 'pg_get_serial_sequence' in query:
                    rows=(sequence_rows if sequence_rows is not None else [
                        ('documents','wissensraum.documents_id_seq',True),
                        ('chunks','wissensraum.chunks_id_seq',True),
                        ('ai_runs','wissensraum.ai_runs_id_seq',True),
                    ])
                    return FakeResult(rows)
                if denied_fragment and denied_fragment in query:
                    raise PermissionError('synthetic permission denied: '+denied_fragment)
                if query.startswith('INSERT INTO wissensraum.'):
                    table=query.split('wissensraum.',1)[1].split('(',1)[0]
                    inner.pending.add((table,parameters[0] if parameters else None))
                    return FakeResult()
                if query.startswith('SELECT 1 FROM wissensraum.'):
                    table=query.split('wissensraum.',1)[1].split(' ',1)[0]
                    marker=parameters[0]
                    return FakeResult([{'?column?':1}] if (table,marker) in inner.pending else [])
                return FakeResult()
            def rollback(inner): inner.rollback_count += 1; inner.pending.clear()
            def close(inner): inner.closed=True
        return ContractConnection()

    def test_postgres_health_checks_every_application_table_and_rolls_back_without_residue(self):
        connection=self._contract_connection()
        store=PostgresStore({**self.values,'port':5432},'synthetic-db-password',
                            connector=lambda **_:connection,initialize=False)

        self.assertEqual(store.health(),{'backend':'PostgreSQL'})
        self.assertGreaterEqual(connection.rollback_count,2)
        self.assertTrue(connection.closed)
        insert_queries=[query for query,_ in connection.queries if query.startswith('INSERT INTO wissensraum.')]
        for table in ('documents','chunks','ai_runs','settings','email_accounts','email_imports'):
            with self.subTest(table=table):
                self.assertTrue(any('wissensraum.'+table+'(' in query for query in insert_queries))
                self.assertTrue(any(query.startswith('SELECT 1 FROM wissensraum.'+table+' ')
                                    for query,_ in connection.queries))
        identity_inserts={table:next((query,parameters) for query,parameters in connection.queries
                                     if query.startswith('INSERT INTO wissensraum.'+table+'('))
                          for table in ('documents','chunks','ai_runs')}
        for table,(query,parameters) in identity_inserts.items():
            with self.subTest(identity_table=table):
                self.assertIn('OVERRIDING SYSTEM VALUE',query)
                self.assertRegex(query,r'\(id\s*,')
                self.assertNotIn('RETURNING id',query)
                self.assertLess(parameters[0],0)
        probe_ids=[parameters[0] for _,parameters in identity_inserts.values()]
        self.assertEqual(len(set(probe_ids)),3)
        self.assertEqual(identity_inserts['chunks'][1][1],identity_inserts['documents'][1][0])
        self.assertTrue(any('pg_get_serial_sequence' in query and 'has_sequence_privilege' in query
                            for query,_ in connection.queries))
        self.assertFalse(any('nextval' in query.lower() for query,_ in connection.queries))
        self.assertFalse(any('CREATE TEMP TABLE' in query for query,_ in connection.queries))

    def test_postgres_health_fails_when_chunks_write_is_denied_and_rolls_back(self):
        connection=self._contract_connection('INSERT INTO wissensraum.chunks')
        store=PostgresStore({**self.values,'port':5432},'synthetic-db-password',
                            connector=lambda **_:connection,initialize=False)

        with self.assertRaisesRegex(PermissionError,'chunks'):
            store.health()
        self.assertGreaterEqual(connection.rollback_count,1)
        self.assertFalse(connection.pending)
        self.assertTrue(connection.closed)

    def test_postgres_health_fails_when_identity_sequence_use_is_denied_and_rolls_back(self):
        connection=self._contract_connection(sequence_rows=[
            ('documents','wissensraum.documents_id_seq',False),
            ('chunks','wissensraum.chunks_id_seq',True),
            ('ai_runs','wissensraum.ai_runs_id_seq',True),
        ])
        store=PostgresStore({**self.values,'port':5432},'synthetic-db-password',
                            connector=lambda **_:connection,initialize=False)

        with self.assertRaisesRegex(PermissionError,'documents_id_seq'):
            store.health()
        self.assertGreaterEqual(connection.rollback_count,1)
        self.assertFalse(connection.pending)
        self.assertTrue(connection.closed)

    def test_postgres_health_fails_when_identity_sequence_is_missing(self):
        connection=self._contract_connection(sequence_rows=[
            ('documents',None,None),
            ('chunks','wissensraum.chunks_id_seq',True),
            ('ai_runs','wissensraum.ai_runs_id_seq',True),
        ])
        store=PostgresStore({**self.values,'port':5432},'synthetic-db-password',
                            connector=lambda **_:connection,initialize=False)

        with self.assertRaisesRegex(ValueError,'documents'):
            store.health()
        self.assertGreaterEqual(connection.rollback_count,1)
        self.assertFalse(connection.pending)
        self.assertTrue(connection.closed)

    def test_postgres_health_rejects_malformed_column_metadata(self):
        class FakeRows:
            def __init__(self,rows): self.rows=rows
            def fetchall(self): return self.rows
        class MalformedConnection:
            def __init__(self,field,value): self.field=field; self.value=value
            def execute(self,query,parameters=()):
                if 'information_schema.columns' in query:
                    rows=[]
                    for table,columns in PostgresStore.column_contract.items():
                        for column,contract in columns.items():
                            values=list(contract)
                            if (table,column)==self.field:
                                values[self.value[0]]=self.value[1]
                            rows.append((table,column,*values))
                    return FakeRows(rows)
                return FakeRows([])
            def rollback(self): pass
            def close(self): pass
        corruptions=(
            (('documents','title'),(0,'integer')),
            (('documents','title'),(2,'YES')),
            (('documents','created_at'),(3,None)),
            (('documents','id'),(4,'NO')),
        )
        for field,value in corruptions:
            with self.subTest(field=field,value=value),self.assertRaisesRegex(ValueError,'Anwendungsschema'):
                PostgresStore({**self.values,'port':5432},'synthetic-db-password',
                              connector=lambda f=field,v=value,**_:MalformedConnection(f,v),initialize=False).health()

    def test_postgres_health_rejects_missing_contract_object(self):
        class FakeRows:
            def fetchall(self): return []
        class IncompleteConnection:
            def execute(self,*_args,**_kwargs): return FakeRows()
            def rollback(self): pass
            def close(self): pass
        with self.assertRaisesRegex(ValueError,'Anwendungsschema'):
            PostgresStore({**self.values,'port':5432},'synthetic-db-password',
                          connector=lambda **_:IncompleteConnection(),initialize=False).health()


if __name__=='__main__': unittest.main()
