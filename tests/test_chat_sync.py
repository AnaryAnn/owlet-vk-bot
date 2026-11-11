import unittest
from unittest.mock import patch
import app
import native_storage as native
import website_store as store
from test_website_store import MemorySheets


class ChatSyncTests(unittest.TestCase):
    @patch.object(app, 'vk_get_chat_members', return_value=[{'vkId':'456', 'name':'Новый'}])
    def test_command_payload_reaches_native_storage(self, members):
        api = MemorySheets()
        with patch.dict('os.environ', {'NATIVE_STORAGE_ENABLED':'true'}), \
             patch.object(native, '_session'), patch.object(native, 'Sheets', return_value=api), \
             patch.object(app, 'google_post', side_effect=native.stats_post) as post:
            message = app.sync_chat_participants(2000000002)
            first_id = post.call_args.args[0]['syncId']
            self.assertTrue(first_id.startswith('chat-sync:'))
            self.assertIn('Синхронизация состава завершена', message)
            self.assertEqual(api.books[store.SHEET_ID]['Участники'][1][:2], ['Новый', '456'])
            app.sync_chat_participants(2000000002)
            self.assertNotEqual(first_id, post.call_args.args[0]['syncId'])
            self.assertEqual(len(api.books[store.SHEET_ID]['_NativeSync']), 3)

    @patch.object(app, 'google_post')
    @patch.object(app, 'vk_get_chat_members', side_effect=RuntimeError('VK unavailable'))
    def test_vk_failure_does_not_write(self, members, post):
        with self.assertRaises(RuntimeError):
            app.sync_chat_participants(2000000002)
        post.assert_not_called()
