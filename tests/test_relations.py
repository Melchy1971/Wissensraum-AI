"""Relations between documents and the comparison of two versions (Release 2.2, step 8A.4)."""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import Store


def check_relations_contract(case, store):
    """Same behaviour on SQLite and PostgreSQL; ``store`` must be empty."""
    plan, _ = store.add('Wartungsplan', 'Die Therme bekommt jährlich eine Wartung.')
    contract, _ = store.add('Wartungsvertrag', 'Vertrag mit der Heizungsfirma.')
    old, _ = store.add('Plan 2025', 'Wartung im März.')
    before = {doc_id: store.get(doc_id) for doc_id in (plan, contract, old)}

    refers = store.relate(plan, contract, 'references', '  Vertrag   regelt Kosten ')
    replaces = store.relate(plan, old, 'supersedes')
    related = store.relate(old, contract, 'related')  # stored with the smaller id first
    case.assertEqual(store.relations(plan), [
        {'id': refers, 'kind': 'references', 'note': 'Vertrag regelt Kosten', 'direction': 'out',
         'other_id': contract, 'other_title': 'Wartungsvertrag'},
        {'id': replaces, 'kind': 'supersedes', 'note': '', 'direction': 'out', 'other_id': old,
         'other_title': 'Plan 2025'}])
    case.assertEqual([(row['kind'], row['direction'], row['other_id']) for row in store.relations(contract)],
                     [('references', 'in', plan), ('related', 'both', old)])
    case.assertEqual([(row['kind'], row['direction']) for row in store.relations(old)],
                     [('supersedes', 'in'), ('related', 'both')])
    for bad in [(plan, contract, 'references'), (contract, old, 'related'), (plan, plan, 'related'),
                (plan, contract, 'kennt'), (plan, 10**9, 'related'), (plan, contract, 'related', 'x' * 301)]:
        with case.assertRaises(ValueError):
            store.relate(*bad)
    history = [(row['field'], row['old_value'], row['new_value']) for row in store.metadata_history(plan)]
    case.assertEqual(history, [('relation', '', f'references:{contract}'), ('relation', '', f'supersedes:{old}')])
    store.unrelate(replaces)
    case.assertEqual(store.metadata_history(plan)[-1]['old_value'], f'supersedes:{old}')
    with case.assertRaises(ValueError):
        store.unrelate(replaces)
    for doc_id, document in before.items():  # relations never change a document
        case.assertEqual(store.get(doc_id), document)
    store.delete(contract)
    case.assertEqual(store.relations(plan), [])
    case.assertEqual(store.relations(old), [])

    first, _ = store.add('Notiz', 'Zeile eins\nZeile zwei\nZeile drei', filename='notiz.md', versioned=True)
    second, _ = store.add('Notiz', 'Zeile eins\nZeile 2\nZeile drei\nZeile vier', filename='notiz.md', versioned=True)
    result = store.compare(first, second)
    case.assertEqual((result['identical'], result['added'], result['removed'], result['truncated']),
                     (False, 2, 1, False))
    case.assertEqual([line for line in result['diff'] if line[:1] in '+-' and line[:3] not in ('+++', '---')],
                     ['-Zeile zwei', '+Zeile 2', '+Zeile vier'])
    case.assertEqual(store.compare(first, first)['diff'], [])
    with case.assertRaises(ValueError):
        store.compare(first, 10**9)


class RelationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.store = Store(self.folder / 'wissen.db')

    def tearDown(self):
        self.temp.cleanup()

    def test_contract(self):
        check_relations_contract(self, self.store)

    def test_long_comparison_is_capped(self):
        import core
        first, _ = self.store.add('A', '\n'.join(f'alt {n}' for n in range(30)))
        second, _ = self.store.add('B', '\n'.join(f'neu {n}' for n in range(30)))
        with patch.object(core, 'MAX_DIFF_LINES', 10):
            result = self.store.compare(first, second)
        self.assertEqual((len(result['diff']), result['truncated'], result['added']), (10, True, 30))

    def test_cli(self):
        import cli
        first, _ = self.store.add('Alt', 'eins')
        second, _ = self.store.add('Neu', 'zwei')

        def run(*argv):
            out = io.StringIO()
            with patch.object(cli, 'BASE', self.folder), patch.dict(os.environ, {}, clear=True), \
                    redirect_stdout(out), redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(['--db', str(self.folder / 'wissen.db'), *argv]), 0)
            return json.loads(out.getvalue())
        created = run('relate', str(second), str(first), '--kind', 'supersedes', '--note', 'ersetzt')
        self.assertEqual([(row['id'], row['direction']) for row in run('relations', str(first))],
                         [(created['id'], 'in')])
        self.assertEqual(run('compare', str(first), str(second))['diff'][-2:], ['-eins', '+zwei'])
        run('unrelate', str(created['id']))
        self.assertEqual(run('relations', str(first)), [])


if __name__ == '__main__':
    unittest.main()
