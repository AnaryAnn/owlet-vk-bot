import unittest
from unittest.mock import patch
import app
import native_storage as native
import test_native_storage


class EveningStorageTests(unittest.TestCase):
    setUp = test_native_storage.NativeAdapterTests.setUp
    def test_period_cleanup_and_checkpoint(self):
        for ts, peer in [(90000, '1'), (100001, '1'), (100002, '2')]:
            native.digest_post(dict(action='saveMessage', timestamp=ts, peerId=peer,
                fromId='7', name='Анна Иванова', text='Обсуждали новый проект'))
        data = dict(peerId='1', until=100000)
        result = native.digest_post(dict(data, action='getEditionMessages'))
        self.assertEqual([m['timestamp'] for m in result['messages']], [90000])
        native.digest_post(dict(data, action='completeEdition'))
        self.assertTrue(native.digest_post(dict(data, action='getEditionMessages'))['alreadySent'])
        following = native.digest_post(dict(action='getEditionMessages', peerId='1', until=186400))
        self.assertEqual([m['timestamp'] for m in following['messages']], [100001])
        other = native.digest_post(dict(action='getEditionMessages', peerId='2', until=186400))
        self.assertEqual(len(other['messages']), 1)


class EveningTests(unittest.TestCase):
    def test_small_digest_without_ai_uses_initials(self):
        with patch.object(app, 'OPENROUTER_API_KEY', ''):
            text = app.build_digest([dict(name='Анна Иванова', text='Сегодня закончила большой вышивальный проект!')])
        self.assertIn('Анна И.', text)
        self.assertNotIn('Иванова', text)

    def test_failed_send_keeps_buffer(self):
        with patch.object(app, 'digest_post', return_value={'messages': []}) as storage, \
             patch.object(app, 'build_scheduled_digest', return_value='Выпуск'), \
             patch.object(app, 'vk_send', side_effect=RuntimeError('offline')):
            with self.assertRaises(RuntimeError):
                app.generate_scheduled_digest('evening')
        self.assertEqual(storage.call_count, 1)
        self.assertEqual(storage.call_args.args[0]['action'], 'getEditionMessages')
