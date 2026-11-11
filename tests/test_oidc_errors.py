"""Refused token requests name only the Entra code (hotfix from the VPS, 11.11.2026)."""
import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from oidc import Provider, SignInError

AUTHORITY = 'https://login.example.test/tenant/v2.0'


def refused(description):
    def http(url, form=None):
        if url.endswith('/.well-known/openid-configuration'):
            return {'issuer': AUTHORITY, 'authorization_endpoint': AUTHORITY + '/authorize',
                    'token_endpoint': AUTHORITY + '/token', 'jwks_uri': AUTHORITY + '/keys'}
        body = io.BytesIO(json.dumps({'error': 'invalid_client', 'error_description': description}).encode())
        raise urllib.error.HTTPError(url, 401, 'Unauthorized', {}, body)
    return http


class EntraCodeTests(unittest.TestCase):
    def test_only_the_code_reaches_the_reason(self):
        provider = Provider(AUTHORITY, 'tenant', 'client', 'geheim', 'https://wissen.example.test/auth/callback',
                            http=refused('AADSTS7000215: Invalid client secret provided. Trace ID: 1234'))
        with self.assertRaises(SignInError) as caught:
            provider.redeem('code', 'verifier')
        self.assertEqual(str(caught.exception), 'token_endpoint_AADSTS7000215')
        provider.http = refused('ohne Code')
        with self.assertRaises(SignInError) as caught:
            provider.refresh('token', 'Mail.Read')
        self.assertEqual(str(caught.exception), 'token_endpoint_unknown')


if __name__ == '__main__':
    unittest.main()
