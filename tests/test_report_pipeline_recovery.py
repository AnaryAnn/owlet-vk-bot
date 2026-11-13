import unittest
from unittest.mock import patch
import app
import website_store as store
from test_website_store import MemorySheets


class PipelineRecoveryTests(unittest.TestCase):
    def test_typed_at_name_report_is_processed_and_gets_a_failure_reply(self):
        with patch.object(app, 'VK_GROUP_ID', '17'), patch.object(app, 'SAVE_CHAT_MESSAGES', False), \
             patch.object(app, 'command_event_once', return_value=True), \
             patch.object(app, 'google_save_report', return_value={'success': False}) as save, \
             patch.object(app, 'vk_send') as send:
            app.process_vk_event(dict(type='message_new', group_id=17, event_id='typed-name',
                object={'message': dict(from_id=123, peer_id=2000000002, text='@Робосычик 15,14')}))
        save.assert_called_once_with(123, 15, 14, 'vk-typed-name', attempts=3)
        self.assertIn('Не удалось подтвердить', send.call_args.args[1])
