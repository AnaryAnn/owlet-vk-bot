import threading
import unittest
from contextlib import contextmanager
from unittest.mock import Mock, patch

import app
import native_storage as native
import website_store as store


class LockRecoveryTests(unittest.TestCase):
    def test_parallel_reports_wait_locally_instead_of_colliding_at_google(self):
        first_entered, second_entered, release_first = threading.Event(), threading.Event(), threading.Event()
        @contextmanager
        def remote(api, book):
            if threading.current_thread().name == 'first':
                first_entered.set()
                self.assertTrue(release_first.wait(3))
            else:
                second_entered.set()
            yield
        def write():
            with store.Sheets(Mock()).locked(store.SHEET_ID):
                pass
        with patch.object(store.Sheets, '_locked_remote', remote):
            first = threading.Thread(target=write, name='first')
            second = threading.Thread(target=write, name='second')
            first.start(); self.assertTrue(first_entered.wait(3)); second.start()
            self.assertFalse(second_entered.wait(.05))
            release_first.set(); first.join(3); second.join(3)
        self.assertTrue(second_entered.is_set())

    def test_uncertain_unlock_retries_only_the_same_owner_qualified_deletion(self):
        api = store.Sheets(Mock())
        calls = []
        def batch(book, payload):
            calls.append((book, payload))
            if len(calls) == 2:
                api.uncertain = True
                raise store.Uncertain('lost delete response')
            return {}
        api.batch = batch
        body = Mock()
        with patch.object(store.time, 'sleep'):
            with api.locked('book'):
                body()
        body.assert_called_once()
        self.assertEqual(calls[1], calls[2])
        created = calls[0][1][0]['createDeveloperMetadata']['developerMetadata']
        deleted = calls[2][1][0]['deleteDeveloperMetadata']['dataFilter']['developerMetadataLookup']
        self.assertEqual(deleted['metadataValue'], created['metadataValue'])
        self.assertFalse(api.uncertain)

    def test_uncertain_data_write_does_not_release_lock(self):
        api = store.Sheets(Mock()); api.batch = Mock(return_value={})
        with api.locked('book'):
            api.uncertain = True
        self.assertEqual(api.batch.call_count, 1)

    def test_tagged_report_reaches_save_and_error_reply_without_preliminary_google_write(self):
        with patch.object(app, 'VK_GROUP_ID', '17'), patch.object(app, 'SAVE_CHAT_MESSAGES', False), \
             patch.object(app, 'command_event_once', return_value=True), \
             patch.object(app, 'google_post', side_effect=store.Busy()) as old_check, \
             patch.object(app, 'google_save_report', return_value={'success': False}) as save, \
             patch.object(app, 'vk_send') as send:
            app.process_vk_event(dict(type='message_new', group_id=17, event_id='tagged-report',
                object={'message': dict(from_id=123, peer_id=2000000002, text='[club17|Робосычик] 30, 17')}))
        old_check.assert_not_called(); save.assert_called_once()
        self.assertIn('Не удалось подтвердить', send.call_args.args[1])
