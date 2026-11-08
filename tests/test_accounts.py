"""Accounts, roles and grants (Release 3.0, step 10.2, docs/sicherheit-3.0.md)."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import accounts
from core import Store

TENANT = '00000000-0000-0000-0000-00000000aaaa'  # synthetic ids, no real tenant


def check_accounts_contract(case, store):
    """Visibility and write rules; same on both backends, ``store`` freshly migrated."""
    stock, _ = store.add('Bestand', 'Inhalt vor dem Mehrbenutzerbetrieb')
    shared, _ = store.add('Freigegeben', 'Inhalt mit Freigabe')
    in_collection, _ = store.add('In Sammlung', 'Inhalt über Sammlung')
    collection = store.create_collection('Projekt')
    store.add_to_collection(collection, [in_collection])

    admin_id = accounts.create_account(store, TENANT, 'oid-admin', 'admin', 'Admin')
    editor_id = accounts.create_account(store, TENANT, 'oid-editor', 'editor', 'Bearbeiter')
    reader_id = accounts.create_account(store, TENANT, 'oid-reader', 'reader', 'Leser')
    with case.assertRaises(ValueError):
        accounts.create_account(store, TENANT, 'oid-reader', 'reader')  # same identity twice
    for bad in (('', 'x', 'reader'), (TENANT, ' ', 'reader'), (TENANT, 'x', 'gast')):
        with case.assertRaises(ValueError):
            accounts.create_account(store, *bad)
    admin, editor, reader = (accounts.account(store, i) for i in (admin_id, editor_id, reader_id))
    case.assertEqual(accounts.by_identity(store, TENANT, 'oid-editor')['id'], editor_id)
    case.assertIsNone(accounts.by_identity(store, 'anderer-mandant', 'oid-editor'))

    # without grants: only the administrator sees anything, the old stock included
    case.assertEqual(accounts.visible_document_ids(store, admin), [stock, shared, in_collection])
    case.assertEqual(accounts.visible_document_ids(store, editor), [])
    case.assertEqual(accounts.visible_document_ids(store, reader), [])

    accounts.grant(store, editor_id, 'owner', document_id=shared)
    accounts.grant(store, reader_id, 'read', collection_id=collection)
    accounts.grant(store, reader_id, 'write', document_id=shared)  # a reader never writes
    case.assertEqual(accounts.visible_document_ids(store, editor), [shared])
    case.assertEqual(accounts.visible_document_ids(store, reader), [shared, in_collection])
    case.assertEqual((accounts.can_write(store, editor, shared), accounts.can_write(store, editor, in_collection)),
                     (True, False))
    case.assertEqual(accounts.can_write(store, reader, shared), False)
    case.assertTrue(accounts.can_write(store, admin, stock))

    accounts.grant(store, editor_id, 'read', document_id=shared)  # replaces, does not add
    case.assertEqual([g['access'] for g in accounts.grants(store, editor_id)], ['read'])
    case.assertFalse(accounts.can_write(store, editor, shared))
    accounts.grant(store, editor_id, 'write', collection_id=collection)
    case.assertTrue(accounts.can_write(store, editor, in_collection))
    for bad in ({'document_id': shared, 'collection_id': collection}, {}, {'document_id': 99999}):
        with case.assertRaises(ValueError):
            accounts.grant(store, editor_id, 'read', **bad)
    with case.assertRaises(ValueError):
        accounts.grant(store, editor_id, 'alles', document_id=shared)

    case.assertEqual(accounts.revoke(store, reader_id, document_id=shared), 1)
    store.delete(shared)  # grants on a deleted document go with it
    case.assertEqual([g['document_id'] for g in accounts.grants(store, editor_id)], [None])

    # role and lock changes check the version that was read
    updated = accounts.update_account(store, reader_id, reader['version'], role='editor')
    case.assertEqual((updated['role'], updated['version']), ('editor', reader['version'] + 1))
    with case.assertRaises(accounts.Conflict):
        accounts.update_account(store, reader_id, reader['version'], disabled=True)  # stale version
    locked = accounts.update_account(store, reader_id, updated['version'], disabled=True)
    case.assertEqual(accounts.visible_document_ids(store, locked), [])
    case.assertFalse(accounts.can_write(store, locked, in_collection))
    with case.assertRaises(ValueError):  # the last active administrator stays
        accounts.update_account(store, admin_id, admin['version'], role='reader')
    with case.assertRaises(ValueError):
        accounts.update_account(store, admin_id, admin['version'], disabled=True)
    case.assertEqual([a['id'] for a in accounts.accounts(store)], [admin_id, editor_id, reader_id])


class AccountsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'wissen.db')

    def tearDown(self):
        self.temp.cleanup()

    def test_contract(self):
        check_accounts_contract(self, self.store)

    def test_filter_runs_inside_the_query(self):
        """The condition is plain SQL for a WHERE clause, so ordering and limits come after it."""
        doc, _ = self.store.add('A', 'Inhalt')
        reader = accounts.account(self.store, accounts.create_account(self.store, TENANT, 'oid-r', 'reader'))
        condition, params = accounts.visible_documents(reader, alias='x')
        self.assertIn('x.id IN', condition)
        with self.store.connect() as db:
            rows = db.execute(f'SELECT x.id FROM documents x WHERE {condition} ORDER BY x.id LIMIT 1', params).fetchall()
        self.assertEqual(rows, [])
        accounts.grant(self.store, reader['id'], 'read', document_id=doc)
        with self.store.connect() as db:
            rows = db.execute(f'SELECT x.id FROM documents x WHERE {condition} ORDER BY x.id LIMIT 1', params).fetchall()
        self.assertEqual([r['id'] for r in rows], [doc])

    def test_cli_creates_the_first_administrator(self):
        import io, json
        from contextlib import redirect_stdout
        db = Path(self.temp.name) / 'cli.db'

        def run(*argv):
            out = io.StringIO()
            with redirect_stdout(out):
                code = __import__('cli').main(['--db', str(db), *argv])
            return code, out.getvalue()
        code, out = run('account-add', '--tenant', TENANT, '--object', 'oid-1', '--role', 'admin', '--name', 'Admin')
        self.assertEqual((code, json.loads(out)['role']), (0, 'admin'))
        self.assertEqual([a['object_id'] for a in json.loads(run('account-list')[1])], ['oid-1'])
        from contextlib import redirect_stderr
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as refused:
            run('account-add', '--tenant', TENANT, '--object', 'oid-1', '--role', 'reader')  # same identity
        self.assertEqual(refused.exception.code, 1)

    def test_desktop_store_works_without_accounts(self):
        doc, _ = self.store.add('A', 'Inhalt')
        self.assertEqual(self.store.get(doc)['title'], 'A')
        self.assertEqual(accounts.accounts(self.store), [])


if __name__ == '__main__':
    unittest.main()
