"""Parity roadmap phase 9: interface and accessibility of the web pages.

Light and dark mode (H02), logo (H03), progress of htmx requests (H04), understandable errors instead of a bare
500 (H05), keyboard use and accessible names (H06), narrow screens (W13) and the score of a hit (B05).
The accessibility check is automated for what HTML alone shows: every form field has a name a screen reader
can read, every page one heading of level 1, a skip link and a marked current page. Contrast and the
behaviour of real screen readers are not tested here.
"""
import io
import json
import sys
import unittest
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import audit
import server
import test_rights
from test_rights import COMMON


class Fields(HTMLParser):
    """Form fields without an accessible name, the h1 count and the forms posting through htmx."""

    def __init__(self):
        super().__init__()
        self.labels, self.label_depth, self.fields, self.h1, self.hx_forms = set(), 0, [], 0, []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'label':
            self.label_depth += 1
            if attrs.get('for'):
                self.labels.add(attrs['for'])
        elif tag in ('input', 'select', 'textarea'):
            if attrs.get('type') in ('hidden', 'submit', 'button'):
                return
            named = self.label_depth or attrs.get('aria-label') or attrs.get('title')
            self.fields.append((attrs.get('name'), attrs.get('id'), bool(named)))
        elif tag == 'h1':
            self.h1 += 1
        elif tag == 'form' and attrs.get('hx-post'):
            self.hx_forms.append(attrs)

    def handle_endtag(self, tag):
        if tag == 'label':
            self.label_depth -= 1

    def unnamed(self):
        return [name for name, field_id, named in self.fields if not named and field_id not in self.labels]


def png(width, height):
    from PIL import Image
    output = io.BytesIO()
    Image.new('RGB', (width, height), 'red').save(output, format='PNG')
    return output.getvalue()


class InterfaceTests(test_rights.EndpointTests):
    locals().update({name: None for name in dir(test_rights.EndpointTests) if name.startswith('test_')})

    def pages(self):
        ids = self.ids
        return ['/', '/suche?q=Pumpe', '/fragen', '/sammlungen', f'/sammlungen/{ids["shared_collection"]}', '/graph',
                f'/graph/entitaet/{ids["erik"]}', '/wiedervorlagen', '/notiz', '/hochladen', '/importe',
                f'/dokument/{ids["granted"]}', f'/dokument/{ids["new"]}/vergleich/{ids["old"]}', '/verwaltung',
                '/verwaltung/betrieb', '/verwaltung/einstellungen', '/verwaltung/datenschutz?suche=Pumpe&id=1',
                '/verwaltung/graph', '/dokument/999999']

    def test_every_field_has_a_name_and_every_page_one_heading(self):
        admin = self.clients['oid-admin']
        for path in self.pages():
            with self.subTest(path):
                page = admin.get(path)
                self.assertIn(page.status_code, (200, 404))
                parsed = Fields()
                parsed.feed(page.text)
                self.assertEqual(parsed.unnamed(), [])
                self.assertEqual(parsed.h1, 1)
                self.assertIn('<a class="skip" href="#inhalt">', page.text)
                for form in parsed.hx_forms:  # progress and no double submit (H04)
                    self.assertEqual((form.get('hx-indicator'), form.get('hx-disabled-elt')), ('#laeuft', 'find button'))
        self.assertIn('<a href="/suche" aria-current="page">', admin.get('/suche').text)
        self.assertIn('<a href="/verwaltung" aria-current="page">', admin.get('/verwaltung/betrieb').text)

    def test_theme_per_browser(self):
        reader = self.clients['oid-reader']
        self.assertNotIn('data-theme', reader.get('/').text)  # follows the system setting
        chosen = reader.post('/darstellung', data={'theme': 'dark', 'back': '/suche'})
        self.assertEqual((chosen.status_code, chosen.headers['location']), (303, '/suche'))
        self.assertIn('wr_theme=dark', chosen.headers['set-cookie'])
        self.assertIn('HttpOnly', chosen.headers['set-cookie'])
        self.assertIn('<html lang="de" data-theme="dark">', reader.get('/').text)
        self.assertEqual(reader.post('/darstellung', data={'theme': 'lila'}).status_code, 422)
        self.assertEqual(reader.post('/darstellung', data={'theme': 'light', 'back': '//fremd.example'}).headers['location'], '/')
        reader.post('/darstellung', data={'theme': 'auto'})
        self.assertNotIn('data-theme', reader.get('/').text)
        css = reader.get('/static/style.css').text
        self.assertIn('prefers-color-scheme: dark', css)
        self.assertIn('@media (max-width: 40rem)', css)  # W13

    def test_logo_by_administrators_as_fresh_png(self):
        admin, editor = self.clients['oid-admin'], self.clients['oid-editor']
        self.assertEqual(admin.get('/branding/logo.png').status_code, 404)
        self.assertNotIn('/branding/logo.png', admin.get('/').text)
        upload = {'logo': ('logo.png', png(1200, 300), 'image/png')}
        self.assertEqual(editor.post('/verwaltung/einstellungen/logo', files=upload).status_code, 403)
        self.assertEqual(admin.post('/verwaltung/einstellungen/logo', files=upload).status_code, 200)
        served = admin.get('/branding/logo.png')
        self.assertEqual(served.headers['content-type'], 'image/png')
        from PIL import Image
        self.assertEqual(Image.open(io.BytesIO(served.content)).size, (320, 80))
        self.assertIn('<img class="logo" src="/branding/logo.png"', self.clients['oid-reader'].get('/').text)
        for name, data in (('bild.svg', b'<svg xmlns="http://www.w3.org/2000/svg"><script>x</script></svg>'),
                           ('text.png', b'kein Bild'), ('bild.gif', b'GIF89a\x01\x00\x01\x00\x00\x00\x00;')):
            with self.subTest(name):
                self.assertEqual(admin.post('/verwaltung/einstellungen/logo', files={'logo': (name, data)}).status_code, 422)
        self.assertEqual(admin.post('/verwaltung/einstellungen/logo', data={'remove': '1'}).status_code, 200)
        self.assertEqual(admin.get('/branding/logo.png').status_code, 404)
        changes = [json.loads(e['details']) for e in audit.entries(self.store, 100) if e['target'] == 'settings:logo']
        self.assertEqual(changes, [{'removed': True}, {'removed': False}])

    def test_unexpected_errors_are_understandable(self):
        class OperationalError(Exception):
            pass

        def broken():
            raise RuntimeError('geheimes Detail')

        def away():
            raise OperationalError('host=db password=x')
        self.app.add_api_route('/kaputt', broken)
        self.app.add_api_route('/api/kaputt', broken)
        self.app.add_api_route('/weg', away)
        reader = self.clients['oid-reader']
        page = reader.get('/kaputt')
        self.assertEqual(page.status_code, 500)
        self.assertIn('unerwarteter Fehler', page.text)
        self.assertNotIn('geheimes Detail', page.text)
        self.assertIn("default-src 'self'", page.headers['content-security-policy'])
        fragment = reader.get('/kaputt', headers={'HX-Request': 'true'})
        self.assertEqual(fragment.headers['hx-retarget'], '#meldung')
        self.assertEqual(reader.get('/api/kaputt').json(), {'detail': 'Ein unerwarteter Fehler ist aufgetreten. '
                                                                      'Er ist protokolliert. Bitte erneut versuchen.'})
        unavailable = reader.get('/weg')
        self.assertEqual(unavailable.status_code, 503)
        self.assertIn('Datenbank ist gerade nicht erreichbar', unavailable.text)
        self.assertNotIn('password', unavailable.text)

    def test_hits_show_their_score(self):
        page = self.clients['oid-reader'].get('/suche', params={'q': COMMON}).text
        self.assertIn('Score 0.', page)
        self.assertIn('(Volltext 1)', page)


if __name__ == '__main__':
    unittest.main()
