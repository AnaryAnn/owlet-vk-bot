import unittest
from unittest.mock import patch, Mock
import app
from website_store import Busy, Uncertain


class ReportRecoveryTests(unittest.TestCase):
    def test_busy_is_retried_with_same_report_identity(self):
        with patch.object(app, 'google_post', side_effect=[Busy('busy'), {'success': True}]) as save, \
             patch.object(app.time, 'sleep'):
            result = app.google_save_report(123, 40, 21, 'vk-e')
        self.assertTrue(result['success'])
        self.assertEqual(save.call_count, 2)
        self.assertEqual(save.call_args_list[0], save.call_args_list[1])
    def test_uncertain_write_not_retried_returns_failure(self):
        with app.app.app_context(), patch.object(app, 'google_post', side_effect=Uncertain('unknown')) as save:
            result = app.google_save_report(123, 40, 21, 'vk-e')
        self.assertFalse(result['success']); save.assert_called_once()
    def test_plain_report_storage_error_receives_chat_reply(self):
        event = dict(type='message_new', group_id=17, event_id='report-error', object=dict(message=dict(
            from_id=123, peer_id=2000000002, text='Робосычик 40, 21')))
        with app.app.app_context(), patch.object(app, 'VK_GROUP_ID', '17'), \
             patch.object(app, 'SAVE_CHAT_MESSAGES', False), \
             patch.object(app, 'command_event_once', return_value=True), \
             patch.object(app, 'google_post', side_effect=RuntimeError('storage unavailable')), \
             patch.object(app, 'vk_send') as send:
            app.process_vk_event(event)
        send.assert_called_once()
        self.assertEqual(send.call_args.args[0], 2000000002)
        self.assertIn('подтвердить сохранение', send.call_args.args[1])


class ReadRecoveryTests(unittest.TestCase):
    def test_transient_read_timeout_retries_same_read(self):
        import requests
        from sheets_api import read_response
        response = Mock(); response.json.return_value = {'ok': True}
        session = Mock(); session.get.side_effect = [requests.ReadTimeout(), response]
        with patch('sheets_api.time.sleep'):
            self.assertIs(read_response(session, 'https://example.test/read', params={'ranges': ['A1']}), response)
        self.assertEqual(session.get.call_count, 2)
        self.assertEqual(session.get.call_args_list[0], session.get.call_args_list[1])
        session.post.assert_not_called()
    def test_read_permission_failure_is_not_retried(self):
        import requests
        from sheets_api import read_response
        response = Mock(status_code=403)
        response.raise_for_status.side_effect = requests.HTTPError(response=response)
        session = Mock(); session.get.return_value = response
        with self.assertRaises(requests.HTTPError): read_response(session, 'https://example.test/read')
        session.get.assert_called_once()
    def test_sheet_metadata_cached_per_request_and_invalidated_on_creation(self):
        from website_store import Sheets
        session = Mock(); session.post.return_value.status_code = 200
        api = Sheets(session)
        api.get = Mock(side_effect=[{'sheets': [{'properties': {'sheetId': 1, 'title': 'a'}}]},
                                   {'sheets': [{'properties': {'sheetId': 2, 'title': 'b'}}]}])
        self.assertIn('a', api.tabs('book')); self.assertIn('a', api.tabs('book'))
        self.assertEqual(api.get.call_count, 1)
        api.batch('book', [{'addSheet': {'properties': {'title': 'b'}}}])
        self.assertIn('b', api.tabs('book')); self.assertEqual(api.get.call_count, 2)
    def test_repeated_report_reads_only_matching_saved_result(self):
        from test_website_store import MemorySheets
        from website_store import save_report, SHEET_ID
        api = MemorySheets()
        first, _, _ = save_report(api, 'Тест', 30, 21, 'repeat')
        original = api.values
        calls = []
        def narrow(book, ranges, **kwargs):
            calls.extend(ranges)
            result = original(book, ranges, **kwargs)
            if ranges == ["'_WebsiteReports'!A2:B"]:
                result = [[row[:2] for row in result[0]]]
            return result
        api.values = narrow
        again, _, _ = save_report(api, 'Тест', 30, 21, 'repeat')
        self.assertEqual(first, again)
        self.assertIn("'_WebsiteReports'!A2:B", calls)
        self.assertIn("'_WebsiteReports'!C2", calls)
        self.assertNotIn("'_WebsiteReports'!A2:C", calls)


class ReportHealthTests(unittest.TestCase):
    def test_health_checks_effective_sheet_id_without_exposing_override(self):
        with patch('sheets_api.SHEET_ID', 'other-private-book'):
            data = app.app.test_client().get('/').json
        self.assertFalse(data['statsSpreadsheetMatchesExpected'])
        self.assertNotIn('other-private-book', str(data))
        with patch('sheets_api.SHEET_ID', '1S1tcMNZj84rLAQi_zPrPT-yxP0b6OYXHpo-JLbbbH60'):
            self.assertTrue(app.app.test_client().get('/').json['statsSpreadsheetMatchesExpected'])
