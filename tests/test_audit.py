"""Audit trail (Release 3.0, step 10.5): negative tests T1 (tampering) and R1 (every event recorded)."""
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import audit
from core import Store
from migrations import SQLITE_AUDIT_TRIGGERS, validate_sqlite

TENANT = '00000000-0000-0000-0000-00000000aaaa'


def _set_protection(db, backend, on):
    if backend == 'postgresql':
        db.execute(f'ALTER TABLE audit_log {"ENABLE" if on else "DISABLE"} TRIGGER USER')
    elif on:
        for sql in SQLITE_AUDIT_TRIGGERS.values():
            db.execute(sql)
    else:
        for name in SQLITE_AUDIT_TRIGGERS:
            db.execute(f'DROP TRIGGER {name}')


def _validate(store):
    with store.connect() as db:
        if store.backend == 'postgresql':
            from migrations import _validate_postgres_schema
            _validate_postgres_schema(db, audit_schema_version())
        else:
            validate_sqlite(db, audit_schema_version())
            db.rollback()


def audit_schema_version():
    from migrations import CURRENT_SCHEMA_VERSION
    return CURRENT_SCHEMA_VERSION


def check_audit_contract(case, store):
    """Append, verify and every way of tampering; same on both backends, ``store`` freshly migrated."""
    case.assertEqual(audit.verify(store), {'ok': True, 'entries': 0, 'head': audit.GENESIS})
    for number in range(5):
        audit.record(store, 'sign_in', number + 1, f'account:{number + 1}', '192.0.2.1', {'n': number}, 1000 + number)
    with case.assertRaisesRegex(ValueError, 'Kennwert'):
        audit.record(store, 'ai_question', 1, details={'frage': 'Wer arbeitet wo und warum? ' * 5})
    with case.assertRaisesRegex(ValueError, 'Unbekanntes'):
        audit.record(store, 'irgendwas')
    good = audit.verify(store)
    case.assertEqual((good['ok'], good['entries']), (True, 5))
    case.assertEqual([e['id'] for e in audit.entries(store, 2)], [5, 4])
    with store.connect() as db:
        baseline = [dict(r) for r in db.execute(f'SELECT {",".join(audit.FIELDS)},hash FROM audit_log ORDER BY id')]

    # the database refuses changes, deletions and (PostgreSQL) truncation
    statements = ['UPDATE audit_log SET event=? WHERE id=2', 'DELETE FROM audit_log WHERE id=5']
    if store.backend == 'postgresql':
        statements.append('TRUNCATE audit_log')
    for statement in statements:
        with case.subTest(statement):
            with case.assertRaises(Exception):
                with store.connect() as db:
                    db.execute(statement, ('sign_out',) if '?' in statement else ())
            case.assertTrue(audit.verify(store)['ok'])

    def restore(db):
        db.execute('DELETE FROM audit_log')
        for row in baseline:
            db.execute(f'INSERT INTO audit_log({",".join(audit.FIELDS)},hash) VALUES(?,?,?,?,?,?,?,?,?)',
                       tuple(row[f] for f in (*audit.FIELDS, 'hash')))

    swap = ('UPDATE audit_log SET created_at=?,account_id=?,target=?,details=?,previous_hash=?,hash=? WHERE id=?')
    rehash = baseline[2] | {'event': 'sign_out'}
    tamperings = {
        'changed': (['UPDATE audit_log SET details=? WHERE id=3'], [('{"n":99}',)], 3, 'verändert'),
        'changed and rehashed': (['UPDATE audit_log SET event=?,hash=? WHERE id=3'],
                                 [('sign_out', audit.entry_hash(rehash))], 4, 'kette'),
        'deleted': (['DELETE FROM audit_log WHERE id=2'], [()], 2, 'fehlt'),
        'swapped': ([swap, swap], [tuple(baseline[2][f] for f in ('created_at', 'account_id', 'target', 'details',
                                                                  'previous_hash', 'hash')) + (2,),
                                   tuple(baseline[1][f] for f in ('created_at', 'account_id', 'target', 'details',
                                                                  'previous_hash', 'hash')) + (3,)], 2, 'kette'),
    }
    for name, (sqls, params, position, reason) in tamperings.items():
        with case.subTest(name):
            with store.connect() as db:
                _set_protection(db, store.backend, False)
                for sql, values in zip(sqls, params):
                    db.execute(sql, values)
            result = audit.verify(store)
            case.assertEqual((result['ok'], result['id'], result['reason']), (False, position, reason))
            with store.connect() as db:
                restore(db)
                _set_protection(db, store.backend, True)
            case.assertTrue(audit.verify(store)['ok'])

    # removing the newest entries leaves a valid chain; the noted state from an earlier check catches it
    noted = (good['entries'], good['head'])
    with store.connect() as db:
        _set_protection(db, store.backend, False)
        db.execute('DELETE FROM audit_log WHERE id=5')
    case.assertTrue(audit.verify(store)['ok'])
    case.assertEqual(audit.verify(store, noted)['reason'], 'gekürzt')

    # the schema check notices missing protection
    with case.assertRaisesRegex(ValueError, 'Schutz des Audit-Verlaufs'):
        _validate(store)
    with store.connect() as db:
        restore(db)
        _set_protection(db, store.backend, True)
    _validate(store)
    case.assertEqual(audit.verify(store, noted), {'ok': True, 'entries': 5, 'head': good['head']})


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'wissen.db'
        self.store = Store(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def test_contract(self):
        check_audit_contract(self, self.store)

    def test_cli_verifies_and_records(self):
        import cli

        def run(*argv):
            out = io.StringIO()
            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                try:
                    code = cli.main(['--db', str(self.path), *argv])
                except SystemExit as stop:
                    code = stop.code
            return code, out.getvalue()
        run('account-add', '--tenant', TENANT, '--object', 'oid-1', '--role', 'admin')
        doc, _ = self.store.add('Weg damit', 'Inhalt zum Löschen')
        run('privacy-erase', str(doc), '--confirm')
        events = [(e['event'], e['source'], json.loads(e['details'])) for e in audit.entries(self.store)]
        self.assertEqual(events, [('privacy_erase', 'cli', {'documents': [doc]}),
                                  ('account_created', 'cli', {'role': 'admin'})])
        code, out = run('audit-verify')
        state = json.loads(out)
        self.assertEqual((code, state['entries']), (0, 2))
        self.assertEqual(run('audit-verify', '--expect', f'3:{state["head"]}')[0], 1)
        self.assertEqual(run('audit-verify', '--expect', f'2:{state["head"]}')[0], 0)
        self.assertEqual(len(json.loads(run('audit-list', '--limit', '1')[1])), 1)


class AuditEndpointTests(unittest.TestCase):
    """R1: every event type of the server leaves one entry with account, time and source."""

    def setUp(self):
        from test_rights import EndpointTests
        self.case = EndpointTests('test_i1_reads_show_only_visible_documents')
        self.case.setUp()
        self.store, self.ids = self.case.store, self.case.ids

    def tearDown(self):
        self.case.tearDown()

    def events(self):
        return [(e['event'], e['account_id'], e['target']) for e in reversed(audit.entries(self.store, 1000))]

    def test_r1_each_event_type(self):
        from test_rights import SECRET
        import server
        signed_in = self.events()
        self.assertEqual([e[0] for e in signed_in], ['sign_in'] * 3)
        self.assertEqual({e[1] for e in signed_in}, {self.case.admin, self.case.editor, self.case.reader})
        admin, editor, reader = (self.case.clients[o] for o in ('oid-admin', 'oid-editor', 'oid-reader'))
        start = len(signed_in)
        admin.get(f'/api/documents/{self.ids["secret"]}/original')
        reader.get(f'/api/collections/{self.ids["shared_collection"]}/export')
        reader.post('/api/questions', json={'question': f'Was steht im Wartungsplan zu {SECRET}?'})
        created = editor.post('/api/documents', json={'title': 'Notiz', 'content': 'Lüftung prüfen.'}).json()['id']
        editor.patch(f'/api/documents/{created}', json={'title': 'Notiz neu'})
        new = admin.post('/api/accounts', json={'tenant_id': TENANT, 'object_id': 'oid-neu', 'role': 'reader'}).json()['id']
        admin.post('/api/grants', json={'account_id': new, 'access': 'read', 'document_id': created})
        reader.post('/logout', data={server.CSRF_FIELD: reader.headers[server.CSRF_HEADER]})
        reader.get('/auth/callback', params={'code': 'x', 'state': 'erfunden'})
        self.assertEqual(self.events()[start:], [
            ('original_download', self.case.admin, f'document:{self.ids["secret"]}'),
            ('collection_export', self.case.reader, f'collection:{self.ids["shared_collection"]}'),
            ('ai_question', self.case.reader, ''),
            ('document_created', self.case.editor, f'document:{created}'),
            ('document_changed', self.case.editor, f'document:{created}'),
            ('account_created', self.case.admin, f'account:{new}'),
            ('grant_created', self.case.admin, f'document:{created}'),
            ('sign_out', self.case.reader, ''),
            ('sign_in_refused', None, ''),
        ])
        rows = audit.entries(self.store, 1000)
        self.assertTrue(all(r['source'] == 'testclient' and r['created_at'] > 1_700_000_000 for r in rows))
        stored = json.dumps(rows, ensure_ascii=False)
        for content in (SECRET, 'Wartungsplan', 'Lüftung', 'Notiz neu', 'Originalbytes'):
            self.assertNotIn(content, stored)  # ids and codes only, never texts or questions
        self.assertEqual(json.loads(rows[0]['details']), {'reason': 'state'})
        self.assertTrue(audit.verify(self.store)['ok'])


if __name__ == '__main__':
    unittest.main()
