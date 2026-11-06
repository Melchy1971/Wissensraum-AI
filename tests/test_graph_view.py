"""Graph view with filters (Release 2.3, step 8B.4)."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import Store

try:
    import tkinter  # noqa: F401  graph_view is a Tk window; the store contract runs without Tk
except ImportError:
    tkinter = None

TEXT = ('Anna Berg arbeitet für die Nordwerk GmbH. Tom Kranz arbeitet für die Nordwerk GmbH. '
        'Tom Kranz verantwortet SAP. Der Einkauf nutzt SAP. Lea Wolf berät die Nordwerk GmbH.')


def build(store):
    types = {row['name']: row['id'] for row in store.entity_types()}
    rtypes = {row['name']: row['id'] for row in store.relation_types()}
    doc, _ = store.add('Organigramm', TEXT)
    ids = {name: store.create_entity(types[kind], name, doc, name)
           for kind, name in [('Person', 'Anna Berg'), ('Person', 'Tom Kranz'), ('Organisation', 'Nordwerk GmbH'),
                              ('System', 'SAP'), ('Prozess', 'Einkauf')]}
    store.relate_entities(ids['Anna Berg'], ids['Nordwerk GmbH'], rtypes['arbeitet für'], doc, 'Anna Berg arbeitet für')
    store.relate_entities(ids['Tom Kranz'], ids['Nordwerk GmbH'], rtypes['arbeitet für'], doc, 'Tom Kranz arbeitet für')
    store.relate_entities(ids['Tom Kranz'], ids['SAP'], rtypes['verantwortet'], doc, 'Tom Kranz verantwortet SAP')
    store.relate_entities(ids['Einkauf'], ids['SAP'], rtypes['nutzt'], doc, 'Der Einkauf nutzt SAP')
    store.propose_entity(types['Person'], 'Lea Wolf', doc, 'Lea Wolf', TEXT.index('Lea Wolf'))
    return types, rtypes, ids, doc


def check_graph_view_contract(case, store):
    """Filters of ``graph_data``; same on both backends, ``store`` freshly migrated."""
    types, rtypes, ids, _ = build(store)
    data = store.graph_data()
    case.assertEqual(len(data['nodes']), 5)  # proposed Lea Wolf hidden by default
    case.assertEqual(len(data['edges']), 4)
    case.assertEqual(data['nodes'][0]['name'], 'Nordwerk GmbH')  # best connected first (tie by name)
    case.assertEqual(len(store.graph_data(include_proposed=True)['nodes']), 6)
    people = store.graph_data(entity_types=[types['Person'], types['Organisation']])
    case.assertEqual({n['name'] for n in people['nodes']}, {'Anna Berg', 'Tom Kranz', 'Nordwerk GmbH'})
    case.assertEqual(len(people['edges']), 2)
    only = store.graph_data(relation_types=[rtypes['verantwortet']])
    case.assertEqual([(e['source'], e['target']) for e in only['edges']], [('Tom Kranz', 'SAP')])
    near = store.graph_data(focus=ids['Anna Berg'], depth=1)
    case.assertEqual({n['name'] for n in near['nodes']}, {'Anna Berg', 'Nordwerk GmbH'})
    wider = store.graph_data(focus=ids['Anna Berg'], depth=2)
    case.assertEqual({n['name'] for n in wider['nodes']}, {'Anna Berg', 'Nordwerk GmbH', 'Tom Kranz'})
    case.assertEqual(wider['nodes'][0]['name'], 'Anna Berg')  # the focus comes first
    named = store.graph_data(query='sap')
    case.assertEqual({n['name'] for n in named['nodes']}, {'SAP', 'Tom Kranz', 'Einkauf'})
    capped = store.graph_data(limit=2)
    case.assertEqual((len(capped['nodes']), capped['truncated']), (2, True))
    case.assertTrue(all(e['source_id'] in {n['id'] for n in capped['nodes']} for e in capped['edges']))
    with case.assertRaises(ValueError):
        store.graph_data(entity_types=[types['System']], focus=ids['Anna Berg'])


class GraphViewTests(unittest.TestCase):
    def test_contract(self):
        with tempfile.TemporaryDirectory() as folder:
            check_graph_view_contract(self, Store(Path(folder) / 'wissen.db'))

    @unittest.skipIf(tkinter is None, 'tkinter fehlt')
    def test_layout_puts_the_focus_in_the_middle_and_everything_inside(self):
        from graph_view import layout
        nodes = [{'id': n} for n in range(1, 8)]
        edges = [{'source_id': 1, 'target_id': 2}, {'source_id': 3, 'target_id': 1}]
        positions = layout(nodes, edges, 800, 600, focus=1)
        self.assertEqual(positions[1], (400, 300))
        inner = math_distance(positions[2], positions[1])
        self.assertAlmostEqual(inner, math_distance(positions[3], positions[1]))
        self.assertLess(inner, math_distance(positions[5], positions[1]))
        self.assertTrue(all(0 < x < 800 and 0 < y < 600 for x, y in positions.values()))
        self.assertEqual(layout([], [], 800, 600), {})
        self.assertEqual(layout([{'id': 9}], [], 800, 600, focus=None), {9: (400, 300)})


def math_distance(a, b):
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


if __name__ == '__main__':
    unittest.main()
