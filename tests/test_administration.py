"""Parity roadmap phase 5: administration, data protection and operations on the server.

Operations overview, integrity and diagnosis (G01, G02, G07), expiry warning of the client secret (S-11),
budgets, retention, import rules and section lengths (C05, E04, D11, D13), selective erasure by
administrators (E03), disclosure per account and installation export through the worker (E02),
and the AI history that withholds answers citing documents no longer visible (S-10). Decisions 11.11.2026.
"""
import datetime
import io
import json
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import accounts
import audit
import budgets
import privacy
import retention
import server
import services
import worker
from core import AI, Store
import test_rights
from test_rights import AUTHORITY, BASE, CLIENT_ID, CLIENT_SECRET, COMMON, SECRET, TENANT, fill, people


def records(store):
    return lambda event, account_id, target, **details: audit.record(store, event, account_id, target, details=details)


def check_administration_contract(case, store, folder):
    """Withheld history, disclosure, export job, erasure and settings with audit alike on both backends."""
    ids = fill(store)
    admin, editor, reader = people(store, ids)
    admin_account, reader_account = accounts.account(store, admin), accounts.account(store, reader)
    sources = json.dumps({'sources': [{'document_id': ids['granted'], 'title': 'Freigegeben'}]})
    with store.connect() as db:
        db.execute('INSERT INTO ai_runs(kind,question,answer,sources_json,model,account_id) VALUES(?,?,?,?,?,?)',
                   ('question', 'Was gilt?', 'Wartungsplan Nordhalle.', sources, 'm', reader))
    as_reader = store.as_viewer(reader_account)
    as_reader.set_favorite(ids['granted'])
    case.assertEqual([(r['answer'], r['withheld']) for r in as_reader.visible_history()], [('Wartungsplan Nordhalle.', False)])
    grant = next(g for g in accounts.grants(store, reader) if g['document_id'] == ids['granted'])
    accounts.revoke(store, reader, document_id=ids['granted'], revision=grant['revision'])
    as_reader = store.as_viewer(accounts.account(store, reader))
    case.assertEqual([(r['answer'], r['withheld'], r['sources']) for r in as_reader.visible_history()], [('', True, [])])
    report = privacy.account_report(store, accounts.account(store, reader))
    case.assertEqual(report['ki_verlauf'][0]['antwort'], None)
    case.assertEqual(report['favoriten'], [{'dokument': ids['granted'], 'titel': None}])  # no title once invisible
    text = json.dumps(report, ensure_ascii=False, default=str)
    case.assertNotIn(SECRET, text)
    case.assertNotIn('Wartungsplan', text)
    case.assertEqual(report['konto']['id'], reader)
    # Export job: one at a time, compare-and-set between server and worker.
    record = records(store)
    services.request_export(store, record, admin_account, 1000)
    with case.assertRaises(ValueError):
        privacy.request_export(store, admin, 1001)
    with store.connect() as db:  # a stale state never wins: only one of server and worker changes it
        case.assertFalse(privacy._swap_state(db, {'status': 'done'}, {'status': 'failed'}))
        case.assertFalse(privacy._swap_state(db, None, {'status': 'pending'}))
    done = privacy.run_export(store, folder, 1002)
    case.assertEqual(done['status'], 'done')
    with zipfile.ZipFile(privacy.export_file(store, folder)) as archive:
        case.assertIn('manifest.json', archive.namelist())
        case.assertNotIn('daten/web_sessions.json', archive.namelist())
    case.assertEqual(privacy.run_export(store, folder, 1002 + privacy.EXPORT_KEEP), {})  # removed after a day
    case.assertEqual(list(Path(folder).glob('datenexport-*')), [])
    # Erasure with audit entry in one transaction.
    counts = services.erase_documents(store, record, admin_account, [ids['secret']], 'LÖSCHEN')
    case.assertEqual(counts['dokumente'], 1)
    with case.assertRaises(ValueError):
        store.get(ids['secret'])
    case.assertEqual(audit.entries(store, 1)[0]['event'], 'privacy_erase')
    with case.assertRaises(ValueError):
        services.erase_documents(store, record, admin_account, [ids['granted']], 'ja')
    services.set_budget(store, record, admin_account, 'ollama', tokens=5000)
    case.assertEqual(budgets.limits(store, 'ollama')['tokens'], 5000)
    services.set_retention(store, record, admin_account, 'ki_verlauf', 30)
    case.assertEqual(retention.rules(store)['ki_verlauf'], 30)
    case.assertTrue(audit.verify(store)['ok'])


class ContractTests(unittest.TestCase):
    def test_contract(self):
        with tempfile.TemporaryDirectory() as folder:
            check_administration_contract(self, Store(Path(folder) / 'wissen.db'), Path(folder) / 'exporte')

    def test_secret_warning(self):
        today = datetime.date(2026, 11, 11)
        self.assertEqual(server.secret_warning(None, today)[0], 'unbekannt')
        self.assertIsNone(server.secret_warning(datetime.date(2027, 1, 1), today))
        self.assertEqual(server.secret_warning(datetime.date(2026, 12, 1), today)[0], 'bald')
        self.assertEqual(server.secret_warning(datetime.date(2026, 11, 10), today)[0], 'abgelaufen')
        self.assertEqual(server.secret_expiry({'WR_OIDC_CLIENT_SECRET_EXPIRES': '2027-03-01'}), datetime.date(2027, 3, 1))
        with self.assertRaises(ValueError):
            server.secret_expiry({'WR_OIDC_CLIENT_SECRET_EXPIRES': '01.03.2027'})


class AdministrationTests(test_rights.EndpointTests):
    locals().update({name: None for name in dir(test_rights.EndpointTests) if name.startswith('test_')})

    def setUp(self):
        super().setUp()
        self.exports = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.rebuild(export_dir=self.exports, secret_expires=datetime.date.today() + datetime.timedelta(days=10))

    def rebuild(self, **options):
        provider = test_rights.Provider(AUTHORITY, TENANT, CLIENT_ID, CLIENT_SECRET, BASE + '/auth/callback',
                                        http=self.idp.http)
        ai = lambda scoped: AI(scoped, 'test-key', transport=test_rights.answer(self.payloads))  # noqa: E731
        self.app = server.create_app(self.store, provider, BASE, ai=ai, **options)
        self.clients = {oid: self.sign_in(oid) for oid in ('oid-admin', 'oid-editor', 'oid-reader')}

    def events(self, name):
        return [json.loads(e['details']) | {'target': e['target']} for e in reversed(audit.entries(self.store, 1000))
                if e['event'] == name]

    def test_only_administrators(self):
        pages = ['/verwaltung/betrieb', '/verwaltung/einstellungen', '/verwaltung/datenschutz',
                 '/verwaltung/datenschutz/export/datei']
        posts = ['/verwaltung/betrieb/pruefen', '/verwaltung/einstellungen/budget', '/verwaltung/einstellungen/aufbewahrung',
                 '/verwaltung/einstellungen/aufbewahrung/anwenden', '/verwaltung/einstellungen/abschnitte',
                 '/verwaltung/einstellungen/regeln', '/verwaltung/einstellungen/regeln/1/loeschen',
                 '/verwaltung/einstellungen/regeln-anwenden', '/verwaltung/einstellungen/regellauf/1/zuruecknehmen',
                 '/verwaltung/datenschutz/loeschen', '/verwaltung/datenschutz/export', '/verwaltung/datenschutz/export/loeschen']
        before = len(audit.entries(self.store, 1000))
        for oid in ('oid-editor', 'oid-reader'):
            client = self.clients[oid]
            for path in pages:
                with self.subTest(oid=oid, path=path):
                    self.assertEqual(client.get(path).status_code, 403)
            for path in posts:
                with self.subTest(oid=oid, path=path):
                    self.assertEqual(client.post(path, data={'id': self.ids['granted'], 'confirmation': 'LÖSCHEN'}).status_code, 403)
        self.assertIsNotNone(self.store.get(self.ids['granted']))
        self.assertEqual(len(audit.entries(self.store, 1000)), before)

    def test_operations_overview_integrity_and_secret_warning(self):
        admin = self.clients['oid-admin']
        page = admin.get('/verwaltung/betrieb').text
        self.assertIn('Dokumente: 5', page)
        self.assertIn('läuft in 10 Tagen ab', page)
        for text in (SECRET, 'Nordhalle', 'Erik Nord'):
            self.assertNotIn(text, page)  # counts only, no content
        self.assertIn('läuft in 10 Tagen ab', admin.get('/verwaltung').text)
        checked = admin.post('/verwaltung/betrieb/pruefen').text
        self.assertIn('Keine Probleme gefunden', checked)
        with self.store.connect() as db:
            db.execute("UPDATE documents SET sha256='0' WHERE id=?", (self.ids['old'],))
        broken = admin.post('/verwaltung/betrieb/pruefen').text
        self.assertIn(f'Prüfsumme weicht ab bei Dokument {self.ids["old"]}', broken)

    def test_settings_with_audit(self):
        admin = self.clients['oid-admin']
        self.assertEqual(admin.post('/verwaltung/einstellungen/budget', data={'provider': 'ollama', 'tokens': '1000',
                                                                          'eur': ''}).status_code, 200)
        self.assertEqual(budgets.limits(self.store, 'ollama')['tokens'], 1000)
        self.assertEqual(admin.post('/verwaltung/einstellungen/budget', data={'provider': 'ollama', 'tokens': '-1'}).status_code, 422)
        self.assertEqual(admin.post('/verwaltung/einstellungen/budget', data={'provider': 'gibtsnicht', 'tokens': '1'}).status_code, 422)
        with self.store.connect() as db:
            db.execute("INSERT INTO ai_runs(kind,question,answer,sources_json,model,created_at,account_id) "
                       "VALUES('question','alt','alt','[]','m','2020-01-01 00:00:00',?)", (self.reader,))
        admin.post('/verwaltung/einstellungen/aufbewahrung', data={'category': 'ki_verlauf', 'days': '30'})
        self.assertIn('ai_runs 1', admin.get('/verwaltung/einstellungen').text)
        self.assertIn('Entfernt', admin.post('/verwaltung/einstellungen/aufbewahrung/anwenden').text)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) AS n FROM ai_runs').fetchone()['n'], 0)
        rule = {'name': 'Kessel', 'keyword': 'Kesselhaus', 'category': 'Technik', 'tags': 'Kessel', 'enabled': '1'}
        self.assertEqual(admin.post('/verwaltung/einstellungen/regeln', data=rule).status_code, 200)
        self.assertEqual(admin.post('/verwaltung/einstellungen/regeln', data={'name': 'leer'}).status_code, 422)
        self.assertIn('Auf den Bestand angewendet ändern die Regeln 2 Dokumente', admin.get('/verwaltung/einstellungen').text)
        self.assertIn('2 Dokumente geändert', admin.post('/verwaltung/einstellungen/regeln-anwenden').text)
        self.assertEqual(self.store.get(self.ids['old'])['category'], 'Technik')
        run = self.events('settings_changed')[-1]['run']
        admin.post(f'/verwaltung/einstellungen/regellauf/{run}/zuruecknehmen')
        self.assertEqual(self.store.get(self.ids['old'])['category'], 'Allgemein')
        self.assertEqual(self.store.document_tags(self.ids['old']), [])
        changed = admin.post('/verwaltung/einstellungen/abschnitte', data={'type': 'txt', 'size': '400', 'overlap': '50'})
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(self.store.chunk_strategies(), {'txt': (400, 50)})
        self.assertEqual(admin.post('/verwaltung/einstellungen/abschnitte', data={'type': 'txt', 'size': '10'}).status_code, 422)
        admin.post('/verwaltung/einstellungen/abschnitte', data={'type': 'txt', 'reset': '1'})
        self.assertEqual(self.store.chunk_strategies(), {})
        targets = [e['target'] for e in self.events('settings_changed')]
        self.assertEqual(targets, ['settings:budget:ollama', 'settings:retention:ki_verlauf', 'settings:retention:apply',
                                   'settings:rule:1', 'settings:rules:apply', f'settings:rules:revert:{run}',
                                   'settings:chunking:txt', 'settings:chunking:txt'])
        self.assertTrue(audit.verify(self.store)['ok'])

    def test_erasure_only_with_confirmation(self):
        admin, doc = self.clients['oid-admin'], self.ids['secret']
        page = admin.get('/verwaltung/datenschutz', params={'suche': SECRET, 'id': doc}).text
        self.assertIn('Betroffen: 1 Dokumente', page)
        refused = admin.post('/verwaltung/datenschutz/loeschen', data={'id': doc, 'confirmation': 'ja'})
        self.assertEqual(refused.status_code, 422)
        self.assertIsNotNone(self.store.get(doc))
        done = admin.post('/verwaltung/datenschutz/loeschen', data={'id': doc, 'confirmation': 'LÖSCHEN'})
        self.assertEqual(done.status_code, 200)
        with self.assertRaises(ValueError):
            self.store.get(doc)
        self.assertEqual(self.events('privacy_erase')[0]['documents'], [doc])
        self.assertNotIn(doc, [h['document_id'] for h in admin.get('/api/search', params={'q': SECRET}).json()['results']])
        self.assertNotIn(f'value="{doc}"', admin.get('/verwaltung/datenschutz', params={'suche': SECRET}).text)

    def test_installation_export_through_the_worker(self):
        admin = self.clients['oid-admin']
        self.assertEqual(admin.post('/verwaltung/datenschutz/export').status_code, 200)
        self.assertEqual(admin.post('/verwaltung/datenschutz/export').status_code, 409)
        self.assertEqual(admin.get('/verwaltung/datenschutz/export/datei').status_code, 404)
        self.assertEqual(worker.export_once(self.store, self.exports)['status'], 'done')
        download = admin.get('/verwaltung/datenschutz/export/datei')
        self.assertEqual(download.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
            self.assertIn('daten/documents.json', archive.namelist())
        self.assertEqual(admin.post('/verwaltung/datenschutz/export/loeschen').status_code, 200)
        self.assertEqual(list(self.exports.iterdir()), [])
        self.assertEqual([e['status'] for e in self.events('privacy_export')], ['requested', 'downloaded', 'discarded'])
        self.rebuild()  # without WR_EXPORT_DIR
        self.assertEqual(self.clients['oid-admin'].post('/verwaltung/datenschutz/export').status_code, 503)

    def test_disclosure_per_account(self):
        reader = self.clients['oid-reader']
        self.store.as_viewer(accounts.account(self.store, self.reader)).set_favorite(self.ids['granted'])
        self.store.as_viewer(accounts.account(self.store, self.editor)).save_search('Fremd', 'Kessel')
        response = reader.get('/konto/auskunft')
        self.assertEqual(response.status_code, 200)
        self.assertIn('attachment', response.headers['content-disposition'])
        report = response.json()
        self.assertEqual(report['konto']['object_id'], 'oid-reader')
        self.assertEqual(report['favoriten'], [{'dokument': self.ids['granted'], 'titel': 'Freigegeben'}])
        self.assertEqual(report['gespeicherte_suchen'], [])
        self.assertNotIn(SECRET, response.text)
        self.assertNotIn('Nordhalle', response.text)  # no content
        self.assertEqual(self.events('privacy_export')[-1], {'status': 'disclosure', 'target': f'account:{self.reader}'})

    def test_history_withholds_answers_after_revoke(self):
        reader = self.clients['oid-reader']
        reader.post('/fragen', data={'question': f'Was gilt für {COMMON}?'})
        self.assertIn('Der Wartungsplan gilt', reader.get('/fragen').text)
        grant = next(g for g in accounts.grants(self.store, self.reader) if g['document_id'] == self.ids['granted'])
        accounts.revoke(self.store, self.reader, document_id=self.ids['granted'], revision=grant['revision'])
        reader = self.sign_in('oid-reader')
        page = reader.get('/fragen').text
        self.assertIn('Antwort ausgeblendet', page)
        self.assertNotIn('Der Wartungsplan gilt', page)
        accounts.grant(self.store, self.reader, 'read', document_id=self.ids['granted'])
        self.assertIn('Der Wartungsplan gilt', self.sign_in('oid-reader').get('/fragen').text)


if __name__ == '__main__':
    unittest.main()
