"""Web server core and sign-in (Release 3.0, step 10.3): negative tests S1 to S3 and E2.

All sign-ins run against a local test identity provider; nothing contacts Microsoft. Tenant,
object ids and keys are synthetic.
"""
import hashlib
import json
import sys
import tempfile
import time
import unittest
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

import accounts
import server
import sessions
from core import Store
from oidc import Provider, SignInError, challenge

TENANT = '00000000-0000-0000-0000-00000000aaaa'
OTHER_TENANT = '00000000-0000-0000-0000-00000000bbbb'
CLIENT_ID = '00000000-0000-0000-0000-0000000c1e17'
CLIENT_SECRET = 'test-client-secret-nicht-echt-0000'
BASE = 'https://wissen.test'
AUTHORITY = f'https://idp.test/{TENANT}/v2.0'
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
FOREIGN_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class TestIdentityProvider:
    """Discovery, JWKS, authorization and token endpoint of a local OpenID provider."""

    def __init__(self):
        self.codes = {}
        self.overrides = {}
        self.signing_key = KEY
        self.algorithm = 'RS256'
        self.calls = []

    def http(self, url, form=None):
        self.calls.append(url)
        if url == AUTHORITY + '/.well-known/openid-configuration':
            return {'issuer': AUTHORITY, 'authorization_endpoint': AUTHORITY + '/authorize',
                    'token_endpoint': AUTHORITY + '/token', 'jwks_uri': AUTHORITY + '/keys'}
        if url == AUTHORITY + '/keys':
            jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(KEY.public_key()))
            jwk.update(kid='k1', use='sig', alg='RS256')
            return {'keys': [jwk]}
        if url == AUTHORITY + '/token':
            entry = self.codes.pop(form['code'], None)
            if (entry is None or form['client_secret'] != CLIENT_SECRET or form['client_id'] != CLIENT_ID
                    or challenge(form['code_verifier']) != entry['challenge']
                    or form['redirect_uri'] != BASE + '/auth/callback'):
                raise ValueError('invalid_grant')
            return {'id_token': self.token(entry['nonce'], entry['oid'])}
        raise OSError('unknown endpoint')

    def authorize(self, url, oid):
        """What the user's browser does at the provider: returns the authorization code."""
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        assert query['code_challenge_method'] == 'S256' and query['client_id'] == CLIENT_ID
        code = f'code-{len(self.codes)}-{oid}'
        self.codes[code] = {'challenge': query['code_challenge'], 'nonce': query['nonce'], 'oid': oid}
        return code, query['state']

    def token(self, nonce, oid):
        now = int(time.time())
        claims = {'iss': AUTHORITY, 'aud': CLIENT_ID, 'tid': TENANT, 'oid': oid, 'nonce': nonce,
                  'iat': now, 'nbf': now, 'exp': now + 3600, 'name': 'Test'}
        claims.update(self.overrides)
        claims = {k: v for k, v in claims.items() if v is not None}
        if self.algorithm == 'HS256':
            return jwt.encode(claims, CLIENT_SECRET, algorithm='HS256', headers={'kid': 'k1'})
        return jwt.encode(claims, self.signing_key, algorithm='RS256', headers={'kid': 'k1'})


def check_sessions_contract(case, store):
    """Sessions and pending sign-ins; same on both backends, ``store`` freshly migrated."""
    account = accounts.create_account(store, TENANT, 'oid-s', 'reader')
    state, nonce, verifier = sessions.start_login(store, 1000)
    case.assertEqual(sessions.finish_login(store, state, 1000 + 599), (sessions.digest(nonce), verifier))
    case.assertIsNone(sessions.finish_login(store, state, 1000 + 600))  # used once
    late, _, _ = sessions.start_login(store, 2000)
    case.assertIsNone(sessions.finish_login(store, late, 2000 + 601))  # expired
    for bad in ('', None, 'x' * 300):
        case.assertIsNone(sessions.finish_login(store, bad, 2000))
    with store.connect() as db:
        stored = [r['state'] for r in db.execute('SELECT state FROM login_requests')]
    case.assertNotIn(state, stored)

    session_id, csrf = sessions.create(store, account, 10_000)
    with store.connect() as db:
        case.assertEqual([r['id'] for r in db.execute('SELECT id FROM web_sessions')],
                         [hashlib.sha256(session_id.encode()).hexdigest()])
    case.assertEqual(sessions.lookup(store, session_id, 10_000 + 1799), {'account_id': account, 'csrf_token': csrf})
    case.assertIsNotNone(sessions.lookup(store, session_id, 10_000 + 3598))  # activity extends it
    case.assertIsNone(sessions.lookup(store, session_id, 10_000 + 3598 + 1801))  # idle 30 minutes
    case.assertIsNone(sessions.lookup(store, session_id, 10_000))  # removed, not just hidden
    long_id, _ = sessions.create(store, account, 50_000)
    for minute in range(25, 8 * 60, 25):
        case.assertIsNotNone(sessions.lookup(store, long_id, 50_000 + minute * 60))
    case.assertIsNone(sessions.lookup(store, long_id, 50_000 + 8 * 3600 + 1))  # absolute 8 hours
    one, _ = sessions.create(store, account, 60_000)
    two, _ = sessions.create(store, account, 60_000)
    accounts.grant(store, account, 'read', document_id=store.add('Doc', 'Inhalt')[0])  # rights change
    case.assertEqual((sessions.lookup(store, one, 60_001), sessions.lookup(store, two, 60_001)), (None, None))


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'wissen.db')
        self.idp = TestIdentityProvider()
        self.provider = Provider(AUTHORITY, TENANT, CLIENT_ID, CLIENT_SECRET, BASE + '/auth/callback', http=self.idp.http)
        self.now = time.time()
        self.app = server.create_app(self.store, self.provider, BASE, clock=lambda: self.now)
        self.client = self.browser()
        self.admin = accounts.create_account(self.store, TENANT, 'oid-admin', 'admin', 'Admin')
        self.reader = accounts.create_account(self.store, TENANT, 'oid-reader', 'reader', 'Leser')

    def tearDown(self):
        self.temp.cleanup()

    def browser(self):
        return TestClient(self.app, base_url=BASE, follow_redirects=False)

    def sign_in(self, oid='oid-reader', client=None):
        client = client or self.client
        start = client.get('/login')
        self.assertEqual(start.status_code, 303)
        code, state = self.idp.authorize(start.headers['location'], oid)
        return client.get('/auth/callback', params={'code': code, 'state': state})

    def signed_in(self, client=None):
        return 'Angemeldet als' in (client or self.client).get('/').text

    def csrf(self, client=None):
        page = (client or self.client).get('/').text
        return page.split(f'name="{server.CSRF_FIELD}" value="')[1].split('"')[0]

    def test_sessions_contract(self):
        check_sessions_contract(self, self.store)

    def test_sign_in_with_pkce_and_cookie_attributes(self):
        start = self.client.get('/login')
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(start.headers['location']).query))
        self.assertEqual((query['code_challenge_method'], query['redirect_uri'], query['response_type']),
                         ('S256', BASE + '/auth/callback', 'code'))
        self.assertNotEqual(query['state'], query['nonce'])
        code, state = self.idp.authorize(start.headers['location'], 'oid-reader')
        done = self.client.get('/auth/callback', params={'code': code, 'state': state})
        self.assertEqual((done.status_code, done.headers['location']), (303, '/'))
        cookie = next(c for c in done.headers.get_list('set-cookie') if c.startswith(server.SESSION_COOKIE + '='))
        attributes = {part.strip().lower() for part in cookie.split(';')[1:]}
        self.assertTrue({'secure', 'httponly', 'samesite=lax', 'path=/'} <= attributes, attributes)
        self.assertFalse(any(a.startswith('domain') for a in attributes))
        self.assertIn('Angemeldet als Leser (reader)', self.client.get('/').text)
        self.assertNotIn(CLIENT_SECRET, start.headers['location'])

    def assert_refused(self, response, status=401):
        self.assertEqual(response.status_code, status, response.text)
        self.assertFalse(any(c.startswith(server.SESSION_COOKIE + '=') for c in response.headers.get_list('set-cookie')))
        self.assertFalse(self.signed_in())

    def test_s1_forged_or_foreign_tokens_are_refused(self):
        now = int(time.time())
        cases = {
            'foreign signature': {'signing_key': FOREIGN_KEY},
            'symmetric algorithm': {'algorithm': 'HS256'},
            'foreign tenant': {'overrides': {'tid': OTHER_TENANT}},
            'foreign issuer': {'overrides': {'iss': f'https://idp.test/{OTHER_TENANT}/v2.0'}},
            'wrong audience': {'overrides': {'aud': 'andere-anwendung'}},
            'expired': {'overrides': {'exp': now - 121, 'iat': now - 3700, 'nbf': now - 3700}},
            'not yet valid': {'overrides': {'nbf': now + 600}},
            'issued in the future': {'overrides': {'iat': now + 600}},
            'other nonce': {'overrides': {'nonce': 'fremd'}},
            'missing oid': {'overrides': {'oid': None}},
        }
        for name, change in cases.items():
            with self.subTest(name):
                self.idp.signing_key, self.idp.algorithm, self.idp.overrides = KEY, 'RS256', {}
                for attribute, value in change.items():
                    setattr(self.idp, attribute, value)
                self.assert_refused(self.sign_in())
        self.idp.signing_key, self.idp.algorithm, self.idp.overrides = KEY, 'RS256', {}
        self.app.state.failed_sign_ins._hits.clear()
        self.assertEqual(self.sign_in().status_code, 303)  # the same setup works with a valid token

    def test_s1_tolerates_two_minutes_of_clock_skew(self):
        now = int(time.time())
        self.idp.overrides = {'iat': now + 100, 'nbf': now + 100, 'exp': now - 100 + 3600}
        self.assertEqual(self.sign_in().status_code, 303)

    def test_s1_state_is_single_use_bound_to_the_browser_and_expires(self):
        start = self.client.get('/login')
        code, state = self.idp.authorize(start.headers['location'], 'oid-reader')
        stranger = self.browser()  # a callback link sent to someone else
        self.assert_refused(stranger.get('/auth/callback', params={'code': code, 'state': state}))
        self.assert_refused(self.client.get('/auth/callback', params={'code': code, 'state': state}))  # used up
        start = self.client.get('/login')
        code, state = self.idp.authorize(start.headers['location'], 'oid-reader')
        self.now += sessions.LOGIN_SECONDS + 1
        self.assert_refused(self.client.get('/auth/callback', params={'code': code, 'state': state}))
        self.assert_refused(self.client.get('/auth/callback', params={'code': 'x', 'state': 'erfunden'}))
        self.assert_refused(self.client.get('/auth/callback', params={'error': 'access_denied', 'state': state}))

    def test_s1_identity_without_account_or_disabled_gets_no_session(self):
        self.assert_refused(self.sign_in('oid-unbekannt'), 403)  # no self-registration
        self.assertEqual([a['object_id'] for a in accounts.accounts(self.store)], ['oid-admin', 'oid-reader'])
        reader = accounts.account(self.store, self.reader)
        accounts.update_account(self.store, self.reader, reader['version'], disabled=True)
        self.assert_refused(self.sign_in(), 403)

    def test_s2_new_session_id_on_every_sign_in_and_only_its_hash_is_stored(self):
        self.sign_in()
        first = self.client.cookies[server.SESSION_COOKIE]
        self.sign_in()
        second = self.client.cookies[server.SESSION_COOKIE]
        self.assertNotEqual(first, second)
        replay = self.browser()
        replay.cookies.set(server.SESSION_COOKIE, first, domain='wissen.test')
        self.assertFalse(self.signed_in(replay))  # the old id is invalid
        with self.store.connect() as db:
            stored = [r['id'] for r in db.execute('SELECT id FROM web_sessions')]
        self.assertEqual(stored, [hashlib.sha256(second.encode()).hexdigest()])
        planted = self.browser()  # fixation: an id chosen by an attacker never becomes a session
        planted.cookies.set(server.SESSION_COOKIE, 'vom-angreifer-gesetzt', domain='wissen.test')
        self.sign_in(client=planted)
        self.assertNotEqual(planted.cookies[server.SESSION_COOKIE], 'vom-angreifer-gesetzt')

    def test_s2_idle_and_absolute_timeout(self):
        self.sign_in()
        self.now += sessions.IDLE_SECONDS - 1
        self.assertTrue(self.signed_in())
        self.now += sessions.IDLE_SECONDS + 1
        self.assertFalse(self.signed_in())

    def test_s2_sign_out_and_lock_end_all_sessions(self):
        laptop, phone = self.browser(), self.browser()
        self.sign_in(client=laptop)
        self.sign_in(client=phone)
        response = laptop.post('/logout', data={server.CSRF_FIELD: self.csrf(laptop)}, headers={'Origin': BASE})
        self.assertEqual(response.status_code, 303)
        self.assertFalse(self.signed_in(laptop))
        self.assertFalse(self.signed_in(phone))
        self.sign_in(client=phone)
        reader = accounts.account(self.store, self.reader)
        accounts.update_account(self.store, self.reader, reader['version'], role='editor')
        self.assertFalse(self.signed_in(phone))
        self.sign_in(client=phone)
        with self.store.connect() as db:  # locked outside the account functions: checked on every request
            db.execute('UPDATE accounts SET disabled=1 WHERE id=?', (self.reader,))
        self.assertFalse(self.signed_in(phone))

    def test_s3_failed_sign_ins_are_limited_per_client(self):
        limit = server.FAILED_SIGN_INS[0]
        for _ in range(limit):
            self.assert_refused(self.client.get('/auth/callback', params={'code': 'x', 'state': 'erfunden'}))
        self.assertEqual(self.client.get('/auth/callback', params={'code': 'x', 'state': 'erfunden'}).status_code, 429)
        self.assertEqual(self.client.get('/login').status_code, 429)
        limiter = server.RateLimit(2, 60, clock=lambda: self.now)
        limiter.record('a'), limiter.record('a')
        self.assertEqual((limiter.blocked('a'), limiter.blocked('b')), (True, False))
        self.now += 61
        self.assertFalse(limiter.blocked('a'))

    def test_e2_state_changes_need_token_and_own_origin(self):
        self.sign_in()
        token = self.csrf()
        attempts = {
            'no token': ({}, {'Origin': BASE}),
            'wrong token': ({server.CSRF_FIELD: 'falsch'}, {'Origin': BASE}),
            'foreign origin': ({server.CSRF_FIELD: token}, {'Origin': 'https://boese.test'}),
            'no origin or referer': ({server.CSRF_FIELD: token}, {}),
            'foreign referer': ({server.CSRF_FIELD: token}, {'Referer': 'https://boese.test/seite'}),
            'look-alike origin': ({server.CSRF_FIELD: token}, {'Referer': BASE + '.boese.test/'}),
        }
        for name, (form, headers) in attempts.items():
            with self.subTest(name):
                self.assertEqual(self.client.post('/logout', data=form, headers=headers).status_code, 403)
                self.assertTrue(self.signed_in())
        anonymous = self.browser()
        self.assertEqual(anonymous.post('/logout', headers={'Origin': BASE}).status_code, 403)
        self.assertEqual(self.client.post('/logout', headers={'Origin': BASE, server.CSRF_HEADER: token}).status_code, 303)
        self.assertFalse(self.signed_in())

    def test_security_headers_and_host_check(self):
        response = self.client.get('/')
        for name, value in (('X-Content-Type-Options', 'nosniff'), ('Referrer-Policy', 'same-origin'),
                            ('X-Frame-Options', 'DENY'), ('Cache-Control', 'no-store'),
                            ('Strict-Transport-Security', 'max-age=31536000')):
            self.assertEqual(response.headers[name], value)
        self.assertIn("script-src 'self'", response.headers['Content-Security-Policy'])
        self.assertNotIn('unsafe-inline', response.headers['Content-Security-Policy'])
        self.assertEqual(self.client.get('/', headers={'Host': 'boese.test'}).status_code, 400)
        self.assertEqual(self.client.get('/docs').status_code, 404)

    def test_provider_outage_is_reported_without_details(self):
        provider = Provider(AUTHORITY, TENANT, CLIENT_ID, CLIENT_SECRET, BASE + '/auth/callback',
                            http=lambda url, form=None: (_ for _ in ()).throw(OSError('kein Netz')))
        app = server.create_app(self.store, provider, BASE)
        response = TestClient(app, base_url=BASE, follow_redirects=False).get('/login')
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('kein Netz', response.text)

    def test_key_rotation_reloads_the_key_set_once(self):
        nonce = 'n'
        token = self.idp.token(nonce, 'oid-reader')
        self.provider.verify(token, sessions.digest(nonce))
        keys_fetched = self.idp.calls.count(AUTHORITY + '/keys')
        unknown = jwt.encode({'a': 1}, KEY, algorithm='RS256', headers={'kid': 'neu'})
        with self.assertRaisesRegex(SignInError, 'unknown_key'):
            self.provider.verify(unknown, sessions.digest(nonce))
        self.assertEqual(self.idp.calls.count(AUTHORITY + '/keys'), keys_fetched + 1)


class AccessLogTests(unittest.TestCase):
    """S-01 (Phase 11.0): the real uvicorn access log keeps paths but no query strings."""

    def test_access_log_has_no_sign_in_code_or_search_words(self):
        import logging
        import socket
        import threading
        import urllib.request
        import uvicorn
        from fastapi import FastAPI
        app = FastAPI()
        for path in ('/auth/callback', '/suche'):
            app.get(path)(lambda: {'ok': True})
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        config = uvicorn.Config(app, host='127.0.0.1', port=port, log_level='info')
        server.quiet_access_log()
        server.quiet_access_log()  # idempotent
        lines = []
        handler = logging.Handler()
        handler.emit = lambda record: lines.append(record.getMessage())
        access = logging.getLogger('uvicorn.access')
        access.addHandler(handler)
        direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        running = uvicorn.Server(config)
        thread = threading.Thread(target=running.run, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not running.started and time.monotonic() < deadline:
                time.sleep(0.05)
            for target in ('/auth/callback?code=Geheimcode-4711&state=Zustand-0815', '/suche?q=Gehaltsliste'):
                direct.open(f'http://127.0.0.1:{port}{target}', timeout=5).close()
        finally:
            running.should_exit = True
            thread.join(10)
            access.removeHandler(handler)
        text = '\n'.join(lines)
        self.assertEqual(sum(1 for f in access.filters if isinstance(f, server.PathOnlyAccessLog)), 1)
        self.assertIn('GET /auth/callback HTTP/1.1', text)
        self.assertIn('/suche', text)
        for secret in ('Geheimcode-4711', 'Zustand-0815', 'code=', 'Gehaltsliste'):
            self.assertNotIn(secret, text)


class ConfigurationTests(unittest.TestCase):
    ENV = {'WR_BASE_URL': 'https://wissen.heimnetz', 'WR_OIDC_TENANT_ID': TENANT, 'WR_OIDC_CLIENT_ID': CLIENT_ID,
           'DB_HOST': 'db', 'DB_DATABASE': 'wissensraum', 'DB_USERNAME': 'wissensraum'}

    def test_secrets_come_from_files_and_https_is_required(self):
        with tempfile.TemporaryDirectory() as folder:
            secret, password = Path(folder) / 'client', Path(folder) / 'db'
            secret.write_text(CLIENT_SECRET + '\n', encoding='utf-8')
            password.write_text('db-test-kennwort\n', encoding='utf-8')
            env = {**self.ENV, 'WR_OIDC_CLIENT_SECRET_FILE': str(secret), 'DB_PASSWORD_FILE': str(password)}
            settings = server.settings_from_env(env)
        self.assertEqual(settings['provider'].redirect_uri, 'https://wissen.heimnetz/auth/callback')
        self.assertEqual(settings['provider'].authority, f'https://login.microsoftonline.com/{TENANT}/v2.0')
        self.assertEqual((settings['db_password'], settings['database']['sslmode']), ('db-test-kennwort', 'require'))
        self.assertNotIn(CLIENT_SECRET, repr(settings['provider']))
        with self.assertRaisesRegex(ValueError, 'HTTPS'):
            server.settings_from_env({**env, 'WR_BASE_URL': 'http://wissen.heimnetz'})
        with self.assertRaisesRegex(ValueError, 'HTTPS'):
            server.settings_from_env({**env, 'WR_BASE_URL': 'http://wissen.heimnetz', 'WR_DEVELOPMENT': '1'})
        self.assertEqual(server.check_base_url('http://localhost:8000', development=True), 'http://localhost:8000')
        with self.assertRaisesRegex(ValueError, 'Pflicht'):
            server.settings_from_env({**self.ENV, 'DB_PASSWORD': 'x'})
        with self.assertRaisesRegex(ValueError, 'ohne Pfad'):
            server.check_base_url('https://wissen.heimnetz/app')


if __name__ == '__main__':
    unittest.main()
