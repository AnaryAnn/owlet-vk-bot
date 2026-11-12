import copy
import json
import threading
import time
import unittest
from contextlib import contextmanager
from unittest.mock import Mock, patch

import requests
import admin_publish as pub
import app


class Drafts:
    def __init__(self):
        self.state = {'phase': 'closed', 'last_cid': 0}
        self.mutex = threading.Lock()
    @contextmanager
    def locked(self):
        with self.mutex:
            store = self
            class Transaction:
                state = copy.deepcopy(store.state)
                def save(self, state):
                    store.state = copy.deepcopy(state)
                    self.state = copy.deepcopy(state)
            yield Transaction()


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.store, self.send = Drafts(), Mock(return_value={'response': 123})
        self.clock = 1000
        self.bot = pub.Publisher(42, self.store, self.send, now=lambda: self.clock)
    def message(self, cid, text='', attachments=None, **extra):
        return self.bot.message(dict(from_id=42, peer_id=42, conversation_message_id=cid,
            text=text, attachments=attachments or [], **extra))
    def draft(self, text='Новость', photos=None):
        self.message(1, 'СЫЧ, НЕСИ ДЕПЕШУ')
        self.message(2, text, photos)
        return self.event()
    def event(self, action='send'):
        return dict(user_id=42, peer_id=42, event_id='event', payload=dict(
            kind='admin_publish', nonce=self.store.state.get('nonce'), action=action))
    def posts(self):
        return [call for call in self.send.call_args_list if call.args[0] == pub.TARGET]

    def test_only_owner_private_messages_can_open_mode(self):
        for sender, peer in [(43, 43), (42, pub.TARGET), (-42, 42)]:
            self.assertFalse(self.bot.message(dict(from_id=sender, peer_id=peer,
                conversation_message_id=1, text=pub.PHRASE)))
        self.send.assert_not_called()

    def test_preview_then_confirmation_posts_exact_content_and_closes(self):
        photos = [{'type': 'photo', 'photo': {'owner_id': 42, 'id': 7, 'access_key': 'abc'}}]
        event = self.draft('Привет!', photos)
        self.assertEqual(self.posts(), [])
        self.assertTrue(all(c.args[0] == 42 for c in self.send.call_args_list))
        self.bot.callback(event)
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual(self.posts()[0].args, (pub.TARGET, 'Привет!'))
        self.assertEqual(self.posts()[0].kwargs['attachment'], 'photo42_7_abc')
        self.assertEqual(self.store.state['phase'], 'closed')
        self.assertNotIn('attachment', self.store.state)

    def test_duplicate_click_after_restart_does_not_post_again(self):
        event = self.draft()
        self.bot.callback(event)
        restarted = pub.Publisher(42, self.store, self.send, now=lambda: self.clock)
        restarted.callback(event)
        self.assertEqual(len(self.posts()), 1)

    def test_private_text_buttons_publish_only_the_matching_draft(self):
        self.draft('Пост')
        nonce = self.store.state['nonce']
        key = pub.keyboard(nonce)['buttons'][0][0]['action']
        self.assertEqual(key['type'], 'text')
        self.message(3, key['label'])
        self.message(4, key['label'])
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual(self.posts()[0].args[1], 'Пост')
        self.assertEqual(self.store.state['phase'], 'closed')

    def test_old_private_button_does_not_publish_a_newer_draft(self):
        self.draft('Первый')
        label = pub.keyboard(self.store.state['nonce'])['buttons'][0][0]['action']['label']
        self.message(3, 'Второй')
        self.message(4, label)
        self.assertFalse(self.posts())
        self.assertEqual(self.store.state['text'], 'Второй')

    def test_photo_only_and_multiple_photos(self):
        photos = [{'type': 'photo', 'photo': {'owner_id': -17, 'id': n}} for n in (1, 2)]
        event = self.draft('', photos)
        self.bot.callback(event)
        self.assertEqual(self.posts()[0].args[1], '')
        self.assertEqual(self.posts()[0].kwargs['attachment'], 'photo-17_1,photo-17_2')

    def test_cancel_and_expiry_do_not_publish(self):
        self.draft()
        self.bot.callback(self.event('cancel'))
        self.bot.callback(self.event())
        self.assertFalse(self.posts())
        self.message(3, pub.PHRASE); self.message(4, 'Ещё')
        self.clock += pub.TTL + 1
        self.bot.callback(self.event())
        self.assertFalse(self.posts()); self.assertEqual(self.store.state['phase'], 'closed')

    def test_edit_invalidates_old_button_and_message_replay_cannot_restore_old_draft(self):
        old = self.draft('Первый')
        self.message(3, 'Второй')
        self.bot.callback(old)
        self.message(2, 'Первый')
        self.bot.callback(self.event())
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual(self.posts()[0].args[1], 'Второй')

    def test_wrong_owner_chat_or_nonce_cannot_confirm(self):
        event = self.draft()
        for key, value in [('user_id', 43), ('peer_id', pub.TARGET)]:
            forged = copy.deepcopy(event); forged[key] = value
            self.bot.callback(forged)
        forged = copy.deepcopy(event); forged['payload']['nonce'] = 'guessed'
        self.bot.callback(forged)
        self.assertFalse(self.posts())

    def test_timeout_retry_uses_frozen_content_and_same_vk_random_id(self):
        event = self.draft('Пост')
        attempts = []
        def send(peer, text, **kwargs):
            if peer == pub.TARGET:
                attempts.append((text, kwargs['random_id']))
                if len(attempts) == 1:
                    raise requests.Timeout()
            return {'response': 12}
        self.bot.send = send
        self.bot.callback(event)
        self.assertEqual(self.store.state['phase'], 'sending')
        self.message(3, 'Изменённый пост')
        self.bot.callback(self.event('cancel'))
        self.clock += pub.TTL * 2
        pub.Publisher(42, self.store, send, now=lambda: self.clock).callback(event)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0], attempts[1])
        self.assertEqual(self.store.state['phase'], 'closed')

    def test_vk_rejection_allows_fix_or_cancellation(self):
        event = self.draft()
        original = self.send
        def send(peer, text, **kwargs):
            if peer == pub.TARGET:
                raise RuntimeError('VK messages.send error: bad attachment')
            return original(peer, text, **kwargs)
        self.bot.send = send
        self.bot.callback(event)
        self.assertEqual(self.store.state['phase'], 'preview')
        self.message(3, 'Исправлено')
        self.bot.callback(self.event('cancel'))
        self.assertEqual(self.store.state['phase'], 'closed')

    def test_unsupported_attachment_cannot_silently_disappear(self):
        self.draft()
        self.message(3, 'Файл', [{'type': 'doc', 'doc': {'id': 1}}])
        self.assertEqual(self.store.state['text'], 'Новость')
        self.assertFalse(self.posts())


class AuthenticationTests(unittest.TestCase):
    def test_approved_recovery_checks_original_content_and_uses_existing_identity(self):
        from datetime import datetime, timezone
        now = datetime(2026, 11, 12, 19, 6, tzinfo=timezone.utc).timestamp()
        store, send = Drafts(), Mock(return_value={'response': 555})
        store.state = dict(phase='preview', last_cid=20, nonce='a'*32, text='Одобрено',
                           attachment='', expires=now+300)
        actual = dict(id=20, from_id=54341305, peer_id=54341305, text='Одобрено')
        with patch.object(pub, 'GoogleDraftStore', return_value=store), \
             patch.object(pub, 'verified_message', return_value=actual), patch.object(pub.time, 'time', return_value=now):
            pub.recover_confirmed_publication(54341305, send, 'token')
            pub.recover_confirmed_publication(54341305, send, 'token')
        posts = [c for c in send.call_args_list if c.args[0] == pub.TARGET]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0].kwargs['random_id'], 'admin-publish:' + 'a'*32)
        self.assertEqual(store.state['phase'], 'closed')

    def test_verified_global_order_does_not_mix_with_conversation_order(self):
        response = Mock()
        drafts, send = Drafts(), Mock()
        publisher = pub.Publisher(42, drafts, send)
        for mid, cid, text in [(18, 900, pub.PHRASE), (20, 2, 'Текст с картинкой')]:
            actual = dict(id=mid, conversation_message_id=cid, from_id=42, peer_id=42,
                          date=int(time.time()), text=text, out=0)
            response.json.return_value = {'response': {'items': [actual]}}
            with patch.object(pub.requests, 'post', return_value=response):
                verified = pub.verified_message(dict(id=mid, peer_id=42), 'token', 42)
            publisher.message(verified)
        self.assertEqual(drafts.state['phase'], 'preview')
        self.assertEqual(drafts.state['text'], 'Текст с картинкой')
        self.assertEqual(drafts.state['last_cid'], 20)

    def test_missing_callback_author_routes_to_independent_vk_verification(self):
        with patch.object(app, 'VK_GROUP_ID', '17'), patch.object(pub, 'handle_message') as handle:
            app.process_vk_event({'type': 'message_new', 'group_id': 17,
                'object': {'message': {'id': 20, 'peer_id': 42, 'text': 'Не доверяем этому тексту'}}})
        handle.assert_called_once()

    def test_active_draft_recovery_shows_preview_without_posting(self):
        actual = dict(id=20, from_id=42, peer_id=42, date=int(time.time()), text='Пост', out=0)
        response = Mock(); response.json.return_value = {'response': {'items': [actual]}}
        drafts, send = Drafts(), Mock()
        drafts.state.update(phase='awaiting', expires=time.time()+600, last_cid=18)
        with patch.object(pub.requests, 'post', return_value=response), \
             patch.object(pub, 'verified_message', return_value=dict(actual, conversation_message_id=20)), \
             patch.object(pub, 'GoogleDraftStore', return_value=drafts):
            pub.recover_command(42, send, 'token')
        self.assertEqual(drafts.state['phase'], 'preview')
        self.assertTrue(all(c.args[0] == 42 for c in send.call_args_list))

    def test_private_callback_without_conversation_id_uses_verified_message_id(self):
        actual = dict(id=18, from_id=42, peer_id=42, date=int(time.time()), text=pub.PHRASE, out=0)
        response = Mock(); response.json.return_value = {'response': {'items': [actual]}}
        with patch.object(pub.requests, 'post', return_value=response) as request:
            result = pub.verified_message(dict(actual, text='Forged'), 'token', 42)
        self.assertTrue(request.call_args.args[0].endswith('messages.getById'))
        self.assertEqual(result['conversation_message_id'], 18)
        self.assertEqual(result['text'], pub.PHRASE)

    def test_startup_recovery_only_opens_recent_verified_command_never_posts_to_chat(self):
        actual = dict(id=18, from_id=42, peer_id=42, date=int(time.time()), text=pub.PHRASE, out=0)
        response = Mock(); response.json.return_value = {'response': {'items': [actual]}}
        drafts, send = Drafts(), Mock()
        with patch.object(pub.requests, 'post', return_value=response), \
             patch.object(pub, 'verified_message', return_value=dict(actual, conversation_message_id=18)), \
             patch.object(pub, 'GoogleDraftStore', return_value=drafts):
            pub.recover_command(42, send, 'token')
            pub.recover_command(42, send, 'token')
        self.assertEqual(drafts.state['phase'], 'awaiting')
        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.call_args.args[0], 42)

    def test_verified_content_is_fetched_from_vk_not_callback_body(self):
        actual = dict(from_id=42, peer_id=42, conversation_message_id=3,
                      date=int(time.time()), text='Настоящий текст', out=0)
        response = Mock(); response.json.return_value = {'response': {'items': [actual]}}
        with patch.object(pub.requests, 'post', return_value=response):
            result = pub.verified_message(dict(actual, text='Поддельный текст'), 'token', 42)
        self.assertEqual(result['text'], 'Настоящий текст')

    def test_wrong_author_or_old_message_cannot_authenticate(self):
        response = Mock()
        for changes in ({'from_id': 43}, {'date': 1}, {'out': 1}):
            actual = dict(from_id=42, peer_id=42, conversation_message_id=3, date=int(time.time()), out=0)
            actual.update(changes)
            response.json.return_value = {'response': {'items': [actual]}}
            with patch.object(pub.requests, 'post', return_value=response):
                self.assertIsNone(pub.verified_message(dict(from_id=42, peer_id=42,
                    conversation_message_id=3), 'token', 42))

    def test_organizer_lookup_is_unique_and_accepts_name_order(self):
        response = Mock(); response.values.return_value = [[['Анна Платонова', '42', 'активен']]]
        with patch.object(pub, '_owner', None), patch.object(pub, '_session'), patch.object(pub, 'Sheets', return_value=response):
            self.assertEqual(pub.resolve_owner(), 42)
        response.values.return_value = [[['Анна Платонова', '42'], ['Платонова Анна', '43']]]
        with patch.object(pub, '_owner', None), patch.object(pub, '_session'), patch.object(pub, 'Sheets', return_value=response):
            with self.assertRaises(ValueError):
                pub.resolve_owner()

    def test_private_routing_is_separate_from_chat_votes_and_reports(self):
        with patch.object(app, 'VK_GROUP_ID', '17'), patch.object(pub, 'handle_message') as handle, \
             patch.object(app, 'google_save_report') as report:
            app.process_vk_event({'type': 'message_new', 'group_id': 17,
                'object': {'message': {'from_id': 42, 'peer_id': 42, 'text': pub.PHRASE}}})
        handle.assert_called_once(); report.assert_not_called()
