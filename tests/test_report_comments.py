import unittest
from unittest.mock import patch

import app
import website_store
from report_comments import append_overplan_comment, OVERPLAN_COMMENTS


class ReportCommentTests(unittest.TestCase):
    def test_all_twenty_comments_are_available(self):
        self.assertEqual(len(OVERPLAN_COMMENTS), 20)
        self.assertEqual(len(set(OVERPLAN_COMMENTS)), 20)

    def test_no_comment_at_or_below_plan(self):
        for plan, fact in [(30, 29), (30, 30), (0, 0)]:
            self.assertEqual(append_overplan_comment('Отчёт', plan, fact, 'r'), 'Отчёт')

    def test_overplan_including_zero_plan(self):
        for plan, fact in [(30, 31), (0, 1)]:
            result = append_overplan_comment('Отчёт', plan, fact, 'r')
            self.assertTrue(any(result == 'Отчёт\n\n' + c for c in OVERPLAN_COMMENTS))

    def test_retry_reuses_phrase(self):
        self.assertEqual(append_overplan_comment('Отчёт', 1, 2, 'report-1'),
                         append_overplan_comment('Отчёт', 1, 2, 'report-1'))
        results = {append_overplan_comment('Отчёт', 1, 2, str(i)) for i in range(100)}
        self.assertGreater(len(results), 1)

    @patch.object(app, 'vk_send_reliable')
    @patch.object(app, 'roll_artifact_direct', return_value={
        'dropped': True, 'artifact': {'name': 'Перо', 'description': 'Описание', 'vkPhoto': 'photo-1_2'}})
    def test_vk_comment_after_artifact(self, roll, send):
        app.send_report_confirmation_background(2000000002, '123', 'Тест', 30, 31, 'r', True)
        message = send.call_args.args[1]
        self.assertTrue(any(message.endswith(c) for c in OVERPLAN_COMMENTS))
        self.assertLess(message.index('Описание'), len(message.rsplit('\n\n', 1)[0]))
        self.assertEqual(send.call_args.kwargs['attachment'], 'photo-1_2')

    @patch.object(website_store.requests, 'post')
    def test_site_comment_after_artifact_and_same_payload_on_retry(self, post):
        post.return_value.json.return_value = {'response': 1}
        report = {'requestId': 'website-r', 'name': 'Тест', 'plan': 30, 'fact': 31,
                  'artifact': {'artifact': {'name': 'Перо', 'description': 'Описание'}}}
        website_store.send_notification(report)
        first = post.call_args.kwargs['data'].copy()
        website_store.send_notification(report)
        self.assertEqual(first, post.call_args.kwargs['data'])
        self.assertTrue(any(first['message'].endswith(c) for c in OVERPLAN_COMMENTS))
        self.assertIn('через сайт', first['message'])


if __name__ == '__main__':
    unittest.main()
