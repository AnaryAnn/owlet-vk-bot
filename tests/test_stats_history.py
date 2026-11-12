import base64
import os
import unittest
from unittest.mock import MagicMock, patch
from flask import Flask
import sheets_api as sheets
import website_api as api


class HistoryTests(unittest.TestCase):
    def test_week_titles_and_new_year(self):
        week = sheets._history_week('29.12-04.01.27')
        self.assertEqual(week['weekStart'], '2026-12-29')
        self.assertEqual(week['weekEnd'], '2027-01-04')
        for title in ('Участники', '_NativeJobs', '01.11-07.11.26', '31.02-06.03.26', '10.11-18.11.26'):
            self.assertIsNone(sheets._history_week(title))

    @patch.object(sheets, '_session')
    def test_read_only_history_and_totals(self, factory):
        session = factory.return_value.__enter__.return_value
        titles = MagicMock()
        titles.json.return_value = {'sheets': [{'properties': {'title': t}} for t in
            ['Участники', '03.11-09.11.26', '10.11-16.11.26', '17.11-23.11.26']]}
        values = MagicMock()
        from test_sheets_grid import grid_payload
        values.json.return_value = grid_payload([
            "'03.11-09.11.26'!B2", "'03.11-09.11.26'!A8:D", "'10.11-16.11.26'!B2", "'10.11-16.11.26'!A8:D"], [
            [['1 000']], [['Выбыл позже', '20', '30'], ['План', '10', '0'], ['Пустой']],
            [], [['Сыч', '1', '2,5'], ['Сыч', '0', '1']]])
        session.get.side_effect = [titles, values]
        with patch.object(sheets, 'current_week', return_value={'start': '2026-11-10'}):
            result = sheets.read_stats_history()
        self.assertEqual(len(result['weeks']), 2)
        self.assertEqual(result['weeks'][0]['totalMice'], 30)
        self.assertEqual(result['weeks'][0]['activeOwlets'], 1)
        self.assertEqual(result['weeks'][1]['totalMice'], 3.5)
        self.assertEqual(result['weeks'][1]['activeOwlets'], 1)
        factory.assert_called_once_with()
        session.post.assert_not_called()

    @patch.object(api, 'read_stats_history', return_value={'success': True, 'weeks': []})
    def test_history_auth_cors_cache_and_week_rollover(self, read):
        app = Flask(__name__); app.register_blueprint(api.website_api)
        client = app.test_client()
        api._history_cache.update(data=None, expires=0, week=None)
        headers = {'X-Website-Password': base64.b64encode('тест'.encode()).decode(),
                   'Origin': 'https://anaryann.github.io'}
        with patch.dict(os.environ, {'WEBSITE_SITE_PASSWORD': 'тест'}):
            self.assertEqual(client.get('/api/website/history').status_code, 401)
            read.assert_not_called()
            self.assertEqual(client.options('/api/website/history', headers=headers).status_code, 204)
            with patch.object(api, 'current_week', return_value={'start': '2026-11-10'}):
                response = client.get('/api/website/history', headers=headers)
                self.assertEqual(response.headers['Access-Control-Allow-Origin'], headers['Origin'])
                client.get('/api/website/history', headers=headers)
                self.assertEqual(read.call_count, 1)
                api._history_cache['expires'] = 0
                client.get('/api/website/history', headers=headers)
                self.assertEqual(read.call_count, 2)
            with patch.object(api, 'current_week', return_value={'start': '2026-11-17'}):
                client.get('/api/website/history', headers=headers)
                self.assertEqual(read.call_count, 3)
