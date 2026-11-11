"""OpenID Connect sign-in with Authorization Code and PKCE (Release 3.0, step 10.3).

Provider-neutral. For Microsoft Entra ID the authority is
``https://login.microsoftonline.com/<tenant id>/v2.0``. The ID token is checked for signature (keys
from the provider's JWKS, RS256 only), issuer, audience, ``exp``, ``iat`` and ``nbf`` with at most two
minutes of clock skew (PyJWT also refuses an ``iat`` in the future), the ``nonce`` of this sign-in and
the configured tenant (``tid``). The account is found by ``tid`` and ``oid``
(docs/sicherheit-3.0.md). Error codes are safe to log; token content never is.
"""
import base64
import hashlib
import json
import secrets
import urllib.parse
import urllib.request

import jwt

LEEWAY_SECONDS = 120
TIMEOUT_SECONDS = 10
MAX_RESPONSE = 1_000_000
REQUIRED_CLAIMS = ['exp', 'iat', 'nbf', 'iss', 'aud', 'nonce', 'tid', 'oid']


class SignInError(Exception):
    """A refused sign-in; ``str(error)`` is a short code without token content."""


def challenge(verifier):
    """PKCE S256 code challenge for a verifier."""
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode('ascii')).digest()).rstrip(b'=').decode('ascii')


def http_json(url, form=None):
    """GET (or POST with a form) over HTTPS and decode JSON; the default transport."""
    if not url.startswith('https://'):
        raise ValueError('Anmeldedienst nur über HTTPS.')
    data = urllib.parse.urlencode(form).encode('ascii') if form is not None else None
    request = urllib.request.Request(url, data=data, headers={'Accept': 'application/json'})
    # The scheme is checked above (bandit B310 warns about file: URLs).
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # nosec B310
        return json.loads(response.read(MAX_RESPONSE))


class Provider:
    def __init__(self, authority, tenant_id, client_id, client_secret, redirect_uri, http=http_json):
        self.authority = authority.rstrip('/')
        self.tenant_id, self.client_id = tenant_id, client_id
        self._secret, self.redirect_uri = client_secret, redirect_uri
        self.http = http
        self._config = None
        self._keys = None

    def __repr__(self):
        return f'Provider({self.authority!r}, client_id={self.client_id!r})'

    def config(self):
        """Discovery document, fetched once."""
        if self._config is None:
            config = self.http(self.authority + '/.well-known/openid-configuration')
            if not all(isinstance(config.get(k), str) and config[k].startswith('https://')
                       for k in ('issuer', 'authorization_endpoint', 'token_endpoint', 'jwks_uri')):
                raise SignInError('discovery')
            self._config = config
        return self._config

    def authorize_url(self, state, nonce, verifier, scope='openid profile', redirect_uri=None):
        """Sign-in by default; phase 8 asks with another ``scope`` and ``redirect_uri`` for mailbox access."""
        query = urllib.parse.urlencode({
            'client_id': self.client_id, 'response_type': 'code', 'redirect_uri': redirect_uri or self.redirect_uri,
            'response_mode': 'query', 'scope': scope, 'state': state, 'nonce': nonce,
            'code_challenge': challenge(verifier), 'code_challenge_method': 'S256'})
        return f"{self.config()['authorization_endpoint']}?{query}"

    def redeem(self, code, verifier):
        """Exchange the code for the ID token (confidential client with PKCE)."""
        return self.redeem_tokens(code, verifier)['id_token']

    def redeem_tokens(self, code, verifier, redirect_uri=None):
        """The whole token response of a code; it always carries an ID token."""
        if not isinstance(code, str) or not 0 < len(code) <= 4096:
            raise SignInError('code')
        try:
            tokens = self.http(self.config()['token_endpoint'], {
                'grant_type': 'authorization_code', 'code': code, 'redirect_uri': redirect_uri or self.redirect_uri,
                'client_id': self.client_id, 'client_secret': self._secret, 'code_verifier': verifier})
        except (OSError, ValueError):
            raise SignInError('token_endpoint') from None
        if not isinstance(tokens, dict) or not isinstance(tokens.get('id_token'), str):
            raise SignInError('no_id_token')
        return tokens

    def refresh(self, refresh_token, scope):
        """New tokens for a stored refresh token (phase 8: Graph access for a connected mailbox)."""
        try:
            tokens = self.http(self.config()['token_endpoint'], {
                'grant_type': 'refresh_token', 'refresh_token': refresh_token, 'scope': scope,
                'client_id': self.client_id, 'client_secret': self._secret})
        except (OSError, ValueError):
            raise SignInError('token_endpoint') from None
        if not isinstance(tokens, dict) or not isinstance(tokens.get('access_token'), str):
            raise SignInError('no_access_token')
        return tokens

    def verify(self, id_token, nonce_digest):
        """Checked claims of an ID token, or ``SignInError``."""
        try:
            header = jwt.get_unverified_header(id_token)
        except jwt.PyJWTError:
            raise SignInError('malformed') from None
        if header.get('alg') != 'RS256':
            raise SignInError('algorithm')
        key = self._key(header.get('kid'))
        try:
            claims = jwt.decode(id_token, key, algorithms=['RS256'], audience=self.client_id,
                                issuer=self.config()['issuer'], leeway=LEEWAY_SECONDS,
                                options={'require': REQUIRED_CLAIMS})
        except jwt.ExpiredSignatureError:
            raise SignInError('expired') from None
        except jwt.InvalidAudienceError:
            raise SignInError('audience') from None
        except jwt.InvalidIssuerError:
            raise SignInError('issuer') from None
        except jwt.PyJWTError:
            raise SignInError('invalid') from None
        if claims['tid'] != self.tenant_id:
            raise SignInError('tenant')
        nonce = claims['nonce']
        if not isinstance(nonce, str) or not secrets.compare_digest(
                hashlib.sha256(nonce.encode('utf-8')).hexdigest(), nonce_digest):
            raise SignInError('nonce')
        if not isinstance(claims['oid'], str) or not claims['oid']:
            raise SignInError('oid')
        return claims

    def _key(self, kid):
        # A key the cached set does not know triggers one reload (the provider rotates keys).
        for reload in (self._keys is None, True):
            if reload:
                try:
                    self._keys = jwt.PyJWKSet.from_dict(self.http(self.config()['jwks_uri']))
                except (OSError, ValueError, TypeError, AttributeError, jwt.PyJWTError):
                    raise SignInError('jwks') from None
            for key in self._keys.keys:
                if key.key_id == kid:
                    return key
        raise SignInError('unknown_key')
