import base64
import os
import unittest
from datetime import datetime
from unittest.mock import patch

from flask import Flask
import sheets_api as sheets
import website_api as api


class WebsiteTests(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__)
        app.register_blueprint(api.website_api)
        self.client = app.test_client()
        api._cache.update(data=None, expires=0, week=None)
        self.env = patch.dict(os.environ, {
            'WEBSITE_SITE_PASSWORD': 'тест', 'WEBSITE_ADMIN_PASSWORD': 'админ',
            'WEBSITE_ALLOWED_ORIGIN': 'https://anaryann.github.io'})
        self.env.start()
        self.addCleanup(self.env.stop)

    def get(self, password='тест', origin='https://anaryann.github.io'):
        return self.client.get('/api/website/stats', headers={
            'X-Website-Password': base64.b64encode(password.encode()).decode(),
            'Origin': origin})

    @patch.object(api, 'read_current_stats', return_value={'success': True})
    def test_auth_and_cors(self, read):
        for password in ('', 'wrong'):
            self.assertEqual(self.get(password).status_code, 401)
        for encoded in ('!!!', '/w=='):
            self.assertEqual(self.client.get('/api/website/stats', headers={
                'X-Website-Password': encoded}).status_code, 401)
        read.assert_not_called()
        for password in ('тест', 'админ'):
            r = self.get(password)
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.headers['Access-Control-Allow-Origin'], 'https://anaryann.github.io')
            self.assertEqual(r.headers['Cache-Control'], 'no-store')
        self.assertNotIn('Access-Control-Allow-Origin', self.get(origin='https://example.com').headers)
        self.assertEqual(self.client.options('/api/website/stats').status_code, 204)

    @patch.object(api, 'read_current_stats', return_value={'success': True})
    def test_cache_expiry_and_week_boundary(self, read):
        with patch.object(api, 'current_week', return_value={'start': '2026-11-03'}):
            self.get(); self.get()
            self.assertEqual(read.call_count, 1)
            api._cache['expires'] = 0
            self.get()
            self.assertEqual(read.call_count, 2)
        with patch.object(api, 'current_week', return_value={'start': '2026-11-10'}):
            self.get()
            self.assertEqual(read.call_count, 3)
            read.assert_called_with(week={'start': '2026-11-10'})

    @patch.object(api, 'read_current_stats', side_effect=RuntimeError('test only'))
    def test_load_failure_does_not_cache(self, read):
        with patch('logging.Logger.exception'):
            r = self.get()
        self.assertEqual(r.status_code, 503)
        self.assertFalse(r.json['success'])
        self.assertIsNone(api._cache['data'])


class SheetsTests(unittest.TestCase):
    def test_number_formats(self):
        for value, expected in [('1 234', 1234), ('1\u00a0234,5', 1234.5),
                                ('1\u202f234', 1234), ('', 0), (None, 0)]:
            self.assertEqual(sheets._number(value), expected)
        for value in ('NaN', 'Infinity', '-Infinity', '#VALUE!'):
            with self.assertRaises(ValueError):
                sheets._number(value)

    def test_week_tuesday_and_monday(self):
        for date, start in [(datetime(2026, 11, 9), '2026-11-03'),
                            (datetime(2026, 11, 10), '2026-11-10')]:
            with patch.object(sheets, 'datetime') as clock:
                clock.now.return_value = date
                self.assertEqual(sheets.current_week()['start'], start)

    @patch.object(sheets, '_session')
    def test_batch_read_and_session_cleanup(self, factory):
        session = factory.return_value.__enter__.return_value
        response = session.get.return_value
        response.json.return_value = {'valueRanges': [
            {'values': [['Тест', '123', 'активен'], ['Выбыл', '456', 'выбыл']]},
            {'values': [['100']]},
            {'values': [['Тест', '30', '17', '10.11.2026']]}]}
        result = sheets.read_current_stats()
        self.assertEqual(result['activePeopleCount'], 1)
        self.assertEqual(result['currentRecords'][0]['fact'], 17)
        self.assertEqual(session.get.call_args.kwargs['timeout'], 25)
        self.assertIn('/values:batchGet', session.get.call_args.args[0])
        factory.return_value.__exit__.assert_called_once()
        response.raise_for_status.side_effect = RuntimeError('test only')
        with self.assertRaises(RuntimeError):
            sheets.read_current_stats()
        self.assertEqual(factory.return_value.__exit__.call_count, 2)


if __name__ == '__main__':
    unittest.main()
