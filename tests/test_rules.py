"""Rules for areas and tags (Release 2.0.1, step S2)."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import rules
from core import Store
from import_jobs import ImportJobs


def snapshot(store):
    """Areas, tag links and tag names: everything a rule run may change."""
    with store.connect() as db:
        return ([(row['id'], row['category']) for row in db.execute('SELECT id,category FROM documents ORDER BY id')],
                [(row['document_id'], row['name']) for row in db.execute(
                    'SELECT dt.document_id,t.name FROM document_tags dt JOIN tags t ON t.id=dt.tag_id ORDER BY 1,2')],
                [row['name'] for row in db.execute('SELECT name FROM tags ORDER BY name')])


def save(store, **fields):
    with store.connect() as db:
        return rules.save_rule(db, {'name': fields.pop('name', 'Regel'), **fields})


def mail(store, sender, subject, folder='INBOX', uid='1'):
    content = f'Betreff: {subject}\nVon: {sender}\nAn: markus@example.test\n\n{subject} Text'
    return store.add(subject, content, 'E-Mail · Konto', subject+'.eml', b'raw', ('imap', 'konto', f'{folder}/{uid}'))[0]


def import_and_rules_scenario(test, store, folder):
    """Shared by SQLite and PostgreSQL: import with rules, tag filter, preview, apply, revert."""
    save(store, name='Rechnungen PDF', extension='pdf', path_pattern='Rechnungen', category='Finanzen',
         tags='Rechnung, Steuer')
    save(store, name='Alles aus Rechnungen', path_pattern='*/rechnungen/*', category='Ablage', tags='Archiv')
    save(store, name='Kunde', sender_domain='kunde.example', category='Kunden', tags='Kunde')
    (folder / 'Rechnungen').mkdir()
    import import_fixtures as fx
    (folder / 'Rechnungen' / 'r1.pdf').write_bytes(fx.pdf_with_text('Rechnung Nummer eins'))
    (folder / 'Rechnungen' / 'notiz.txt').write_text('Notiz im Rechnungsordner', encoding='utf-8')
    (folder / 'brief.txt').write_text('Brief ohne Regel', encoding='utf-8')
    jobs = ImportJobs(store)
    test.assertEqual(jobs.run(jobs.create_for_folder(folder))['imported'], 3)
    by_title = {row['title']: row for row in store.list()}
    # First matching rule with an area wins; tags come from every matching rule.
    test.assertEqual(by_title['r1']['category'], 'Finanzen')
    test.assertEqual(by_title['r1']['tags'], ['Archiv', 'Rechnung', 'Steuer'])
    test.assertEqual((by_title['notiz']['category'], by_title['notiz']['tags']), ('Ablage', ['Archiv']))
    test.assertEqual((by_title['brief']['category'], by_title['brief']['tags']), ('Allgemein', []))
    mail_id = mail(store, 'Erika <erika@post.kunde.example>', 'Angebot')
    test.assertEqual(store.get(mail_id)['category'], 'Kunden')
    note_id = store.add('Notiz', 'Rechnung als Notiz', 'Eigenes')[0]
    test.assertEqual(store.get(note_id)['category'], 'Eigenes')  # notes are never changed by rules
    # Tag filter in list and search; tags are matched without regard to case.
    test.assertEqual({row['title'] for row in store.list(tag='archiv')}, {'r1', 'notiz'})
    test.assertEqual([row['title'] for row in store.search('Rechnung', tag='STEUER')], ['r1'])
    test.assertEqual(store.search('Rechnung', tag='Unbekannt'), [])
    with store.connect() as db:
        test.assertEqual(rules.tag_counts(db), [('Archiv', 2), ('Kunde', 1), ('Rechnung', 1), ('Steuer', 1)])

    # Existing documents: a new rule changes nothing until preview and confirmation.
    save(store, name='Briefe', keyword='brief', category='Korrespondenz', tags='Brief')
    before = snapshot(store)
    with store.connect() as db:
        rows = rules.preview(db)
    test.assertEqual(snapshot(store), before)
    test.assertEqual([(row['title'], row['category'], row['new_category'], row['new_tags']) for row in rows],
                     [('brief', 'Allgemein', 'Korrespondenz', ['Brief'])])
    with store.connect() as db:
        result = rules.apply(db, store.backend, rows)
    test.assertEqual((result['changed'], result['skipped']), (1, 0))
    test.assertEqual(store.get(by_title['brief']['id'])['category'], 'Korrespondenz')
    test.assertEqual([row['title'] for row in store.search('Korrespondenz')], ['brief'])  # index follows the area
    with store.connect() as db:
        test.assertEqual(rules.preview(db), [])  # nothing left to do
        test.assertEqual([(run['status'], run['documents']) for run in rules.runs(db)], [('applied', 1)])
        test.assertEqual(rules.revert(db, store.backend, result['run_id']), {'restored': 1, 'conflicts': 0})
    test.assertEqual(snapshot(store), before)  # revert restores the exact previous state
    test.assertEqual(store.search('Korrespondenz'), [])
    with store.connect() as db:
        test.assertEqual(rules.runs(db)[0]['status'], 'reverted')
        with test.assertRaisesRegex(ValueError, 'bereits zurückgenommen'):
            rules.revert(db, store.backend, result['run_id'])


class RuleValidationTests(unittest.TestCase):
    def test_rules_need_name_condition_and_action(self):
        cases = (({'keyword': 'x', 'category': 'A'}, 'Namen'), ({'name': 'R', 'category': 'A'}, 'Bedingung'),
                 ({'name': 'R', 'keyword': 'x'}, 'Bereich oder Tags'),
                 ({'name': 'R', 'sender_domain': 'kein domain', 'category': 'A'}, 'Absenderdomäne'),
                 ({'name': 'R', 'extension': '.p d f', 'category': 'A'}, 'Endung'),
                 ({'name': 'R', 'source_type': 'note', 'category': 'A'}, 'Quellentyp'),
                 ({'name': 'R', 'keyword': 'x', 'tags': ','.join(f't{i}' for i in range(11))}, 'höchstens 10'),
                 ({'name': 'R', 'keyword': 'x', 'tags': 'x' * 41}, 'höchstens 40'))
        for data, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                rules.normalize_rule(data)

    def test_values_are_normalized(self):
        rule = rules.normalize_rule({'name': '  Kunde  A ', 'sender_domain': '@Kunde.Example', 'extension': '.PDF',
                                     'path_pattern': 'C:\\Ablage\\*', 'tags': ' Rechnung ,rechnung,  Steuer  Jahr ,'})
        self.assertEqual((rule['name'], rule['sender_domain'], rule['extension'], rule['path_pattern'], rule['tags']),
                         ('Kunde A', 'kunde.example', 'pdf', 'C:/Ablage/*', 'Rechnung, Steuer Jahr'))


class RuleMatchingTests(unittest.TestCase):
    DOC = {'title': 'Jahresabschluss', 'content': 'Von: Max <max@mail.kunde.example>\n\nBilanz 2026',
           'filename': 'Abschluss.PDF', 'source_type': 'file', 'locator': 'C:\\Daten\\Finanzen\\Abschluss.PDF'}

    def rule(self, **fields):
        return rules.normalize_rule({'name': 'R', 'category': 'A', **fields})

    def test_each_condition(self):
        for fields, expected in (({'source_type': 'file'}, True), ({'source_type': 'imap'}, False),
                                 ({'path_pattern': 'finanzen'}, True), ({'path_pattern': 'Steuern'}, False),
                                 ({'path_pattern': '*/finanzen/*.pdf'}, True), ({'path_pattern': '*/daten/*.pdf'}, True),
                                 ({'path_pattern': 'finanzen/*.docx'}, False),
                                 ({'sender_domain': 'kunde.example'}, True), ({'sender_domain': 'mail.kunde.example'}, True),
                                 ({'sender_domain': 'unde.example'}, False), ({'extension': 'pdf'}, True),
                                 ({'extension': 'docx'}, False), ({'keyword': 'BILANZ'}, True),
                                 ({'keyword': 'jahresabschluss'}, True), ({'keyword': 'Umsatz'}, False)):
            with self.subTest(fields=fields):
                self.assertEqual(rules.matches(self.rule(**fields), self.DOC), expected)

    def test_all_conditions_must_hold(self):
        self.assertTrue(rules.matches(self.rule(extension='pdf', keyword='Bilanz'), self.DOC))
        self.assertFalse(rules.matches(self.rule(extension='pdf', keyword='Umsatz'), self.DOC))

    def test_sender_domain_only_from_the_von_line(self):
        self.assertEqual(rules.sender_domain('Text mit a@kunde.example\nVon: B <b@andere.example>'), 'andere.example')
        self.assertEqual(rules.sender_domain('Kein Absender a@kunde.example'), '')

    def test_order_disabled_rules_and_notes(self):
        first = {**self.rule(keyword='Bilanz', category='Erster', tags='Eins'), 'enabled': 1}
        second = {**self.rule(extension='pdf', category='Zweiter', tags='zwei, eins'), 'enabled': 1}
        tags_only = {**self.rule(keyword='Bilanz', category='', tags='Drei'), 'enabled': 1}
        self.assertEqual(rules.evaluate([tags_only, first, second], self.DOC), ('Erster', ['Drei', 'Eins', 'zwei']))
        self.assertEqual(rules.evaluate([second, first], self.DOC), ('Zweiter', ['zwei', 'eins']))
        self.assertEqual(rules.evaluate([{**first, 'enabled': 0}, second], self.DOC), ('Zweiter', ['zwei', 'eins']))
        self.assertEqual(rules.evaluate([first], {**self.DOC, 'source_type': 'note'}), (None, []))


class RuleStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.store = Store(self.base / 'wissen.db')

    def tearDown(self):
        self.temp.cleanup()

    def test_import_tags_preview_apply_and_revert(self):
        folder = self.base / 'quelle'
        folder.mkdir()
        import_and_rules_scenario(self, self.store, folder)

    def test_moving_a_rule_changes_which_area_wins(self):
        first = save(self.store, name='A', keyword='Vertrag', category='Recht')
        second = save(self.store, name='B', keyword='Vertrag', category='Einkauf')
        self.assertEqual(self.store.get(mail(self.store, 'x@y.example', 'Vertrag', uid='1'))['category'], 'Recht')
        with self.store.connect() as db:
            rules.move_rule(db, second, -1)
            self.assertEqual([rule['id'] for rule in rules.list_rules(db)], [second, first])
            rules.move_rule(db, second, -1)  # already first: stays
            self.assertEqual([rule['position'] for rule in rules.list_rules(db)], [1, 2])
        self.assertEqual(self.store.get(mail(self.store, 'x@y.example', 'Vertrag neu', uid='2'))['category'], 'Einkauf')

    def test_rule_edit_and_delete(self):
        rule_id = save(self.store, keyword='a', category='A')
        with self.store.connect() as db:
            rules.save_rule(db, {'name': 'Neu', 'keyword': 'b', 'tags': 'T', 'enabled': False}, rule_id)
            self.assertEqual([(r['name'], r['keyword'], r['category'], r['tags'], r['enabled'])
                              for r in rules.list_rules(db)], [('Neu', 'b', '', 'T', 0)])
            rules.delete_rule(db, rule_id)
            self.assertEqual(rules.list_rules(db), [])
            with self.assertRaisesRegex(ValueError, 'nicht gefunden'):
                rules.save_rule(db, {'name': 'X', 'keyword': 'b', 'tags': 'T'}, rule_id)

    def test_revert_keeps_a_newer_area_and_skips_rows_changed_since_preview(self):
        doc_a = mail(self.store, 'a@kunde.example', 'Erste', uid='1')
        doc_b = mail(self.store, 'b@kunde.example', 'Zweite', uid='2')
        save(self.store, sender_domain='kunde.example', category='Kunden', tags='Kunde')
        with self.store.connect() as db:
            rows = rules.preview(db)
        self.assertEqual([row['document_id'] for row in rows], [doc_a, doc_b])
        with self.store.connect() as db:
            rules.set_category(db, 'sqlite', doc_b, 'Von Hand')  # changed after the preview
            result = rules.apply(db, 'sqlite', rows)
        self.assertEqual((result['changed'], result['skipped']), (1, 1))
        self.assertEqual(self.store.get(doc_b)['category'], 'Von Hand')
        with self.store.connect() as db:
            rules.set_category(db, 'sqlite', doc_a, 'Später geändert')
            self.assertEqual(rules.revert(db, 'sqlite', result['run_id']), {'restored': 1, 'conflicts': 1})
            self.assertEqual(rules.document_tags(db, doc_a), [])
        self.assertEqual(self.store.get(doc_a)['category'], 'Später geändert')

    def test_preview_without_changes_creates_no_run(self):
        mail(self.store, 'a@kunde.example', 'Erste')
        with self.store.connect() as db:
            self.assertEqual(rules.preview(db), [])
            self.assertEqual(rules.apply(db, 'sqlite', []), {'run_id': None, 'changed': 0, 'skipped': 0})
            self.assertEqual(rules.runs(db), [])

    def test_cli_changes_existing_documents_only_with_confirm(self):
        import io
        import json
        from contextlib import redirect_stdout
        from unittest.mock import Mock, patch
        import cli
        doc = mail(self.store, 'a@kunde.example', 'Erste')
        vault = Mock(); vault.get.return_value = None

        def run(*args):
            output = io.StringIO()
            with patch.object(cli, 'BASE', self.base), patch.object(cli, 'SystemVault', return_value=vault), \
                    redirect_stdout(output):
                self.assertEqual(cli.main(['--db', str(self.store.path), *args]), 0)
            return json.loads(output.getvalue())
        self.assertEqual(run('rule-add', 'Kunde', '--sender-domain', 'kunde.example', '--category', 'Kunden',
                             '--tags', 'Kunde')['rule_id'], 1)
        self.assertEqual(run('rules')[0]['sender_domain'], 'kunde.example')
        self.assertEqual(run('rules-apply')['documents'], 1)  # without --confirm: nothing changes
        self.assertEqual(self.store.get(doc)['category'], 'E-Mail · Konto')
        result = run('rules-apply', '--confirm')
        self.assertEqual((result['changed'], self.store.get(doc)['category']), (1, 'Kunden'))
        self.assertEqual(run('tags'), [{'tag': 'Kunde', 'documents': 1}])
        self.assertEqual([row['id'] for row in run('list', '--tag', 'kunde')], [doc])
        self.assertEqual([row['document_id'] for row in run('search', 'Erste', '--tag', 'Kunde')], [doc])
        self.assertEqual(run('rules-revert', str(result['run_id'])), {'restored': 1, 'conflicts': 0})
        self.assertEqual(run('rules-runs')[0]['status'], 'reverted')
        self.assertEqual(run('rule-delete', '1'), {'deleted': 1})
        self.assertEqual(run('rules'), [])

    def test_deleting_a_document_removes_its_tag_links(self):
        save(self.store, keyword='Erste', tags='Weg')
        doc = mail(self.store, 'a@b.example', 'Erste')
        self.store.delete(doc)
        with self.store.connect() as db:
            self.assertEqual(rules.tag_counts(db), [])


if __name__ == '__main__':
    unittest.main()
