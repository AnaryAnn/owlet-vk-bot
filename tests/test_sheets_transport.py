import unittest
from unittest.mock import patch
from google.auth.credentials import AnonymousCredentials
from sheets_api import SheetsSession


class TransportTests(unittest.TestCase):
    def test_api_and_token_requests_never_read_netrc(self):
        with SheetsSession(AnonymousCredentials()) as session:
            with patch('requests.sessions.get_netrc_auth', side_effect=AssertionError('netrc lookup')):
                for transport in (session, session._owned_auth_session):
                    prepared = transport.prepare_request(__import__('requests').Request(
                        'POST', 'https://oauth2.googleapis.com/token', data='test'))
                    self.assertIsNone(prepared.headers.get('Authorization'))
                    self.assertFalse(transport.trust_env)
                    self.assertTrue(transport.verify)
