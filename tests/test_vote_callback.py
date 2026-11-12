import unittest
import json
from unittest.mock import patch, Mock
import app
import native_quests
from quest_votes import version, options
from test_quest_votes import VotingSheets


class CallbackStorageTests(unittest.TestCase):
    def setUp(self):
        self.api = VotingSheets()
        native_quests.quest_action(self.api, 'b', 'tick', {'fact': 0})
        _, qs = native_quests.table(self.api, 'b', 'Квесты')
        _, ss = native_quests.table(self.api, 'b', 'Этапы')
        self.selection = dict(questId='q', stageId='start', revision=version(qs[0], ss[0], options(ss[0], ss)))
    def cast(self, event, choice):
        return native_quests.quest_action(self.api, 'b', 'vote', dict(self.selection,
            source='callback', eventId=event, choice=choice, peerId='2000000002', vkId='123'))
    def test_repeat_old_event_cannot_revert_changed_vote(self):
        self.cast('first', 1)
        self.cast('second', 2)
        result = self.cast('first', 1)
        self.assertTrue(result['duplicate']); self.assertEqual(result['choice'], 2)
        self.assertEqual(len(self.api.sheets['Голоса']), 2)
        self.assertEqual(len(self.api.sheets['_QuestVoteEvents']), 3)
    def test_vote_and_receipt_saved_in_one_batch(self):
        self.cast('first', 1)
        self.assertEqual(len(self.api.writes[-1]), 2)
    def test_second_click_increments_server_sequence(self):
        self.cast('one', 1); self.cast('two', 2)
        self.assertEqual(self.api.sheets['Голоса'][1][4:6], [2, 2])
    def test_invalid_event_does_not_write_vote(self):
        before = len(self.api.writes)
        with self.assertRaises(ValueError): self.cast('', 1)
        self.assertEqual(len(self.api.writes), before)


class CallbackDeliveryTests(unittest.TestCase):
    def event(self):
        return dict(user_id=123, peer_id=2000000002, event_id='click', payload=dict(
            kind='quest_vote', questId='q', stageId='s', revision='a'*16, choice=2))
    def test_identity_taken_from_event_and_feedback_is_private(self):
        event = self.event(); event['payload'].update(vkId='999', peerId='2000000099')
        with patch('quest_engine.quest_api', return_value={'success': True, 'choice': 2}) as save, \
             patch.object(app, 'vote_feedback') as feedback:
            app.process_quest_vote_callback(event)
        self.assertEqual(save.call_args.kwargs['vkId'], '123')
        self.assertEqual(save.call_args.kwargs['peerId'], '2000000002')
        self.assertEqual(save.call_args.kwargs['source'], 'callback')
        self.assertNotIn('messageId', save.call_args.kwargs)
        self.assertEqual(feedback.call_args.args[:3], (123, 2000000002, 'click'))
    def test_write_failure_does_not_claim_vote_was_accepted(self):
        with app.app.app_context(), patch('quest_engine.quest_api', side_effect=RuntimeError('offline')), \
             patch.object(app, 'vote_feedback') as feedback:
            app.process_quest_vote_callback(self.event())
        self.assertIn('не сохранён', feedback.call_args.args[3])
    def test_dm_denied_still_uses_snackbar_never_chat(self):
        response = Mock(); response.json.return_value = {'response': 1}
        with app.app.app_context(), patch.object(app, 'vk_send', side_effect=RuntimeError('blocked')) as send, \
             patch.object(app.requests, 'post', return_value=response) as api:
            app.vote_feedback(123, 2000000002, 'click', 'Голос принят')
        send.assert_called_once()
        self.assertEqual(send.call_args.args[0], 123)
        data = api.call_args.kwargs['data']
        self.assertIn('sendMessageEventAnswer', api.call_args.args[0])
        self.assertEqual(data['user_id'], 123)
        self.assertEqual(json.loads(data['event_data'])['type'], 'show_snackbar')
    def test_successful_dm_is_sent_only_to_clicking_user(self):
        response = Mock(); response.json.return_value = {'response': 1}
        with patch.object(app, 'vk_send') as send, patch.object(app.requests, 'post', return_value=response):
            app.vote_feedback(123, 2000000002, 'click', 'Принят')
        self.assertEqual(send.call_args.args[:2], (123, 'Принят'))
        self.assertIn('123', send.call_args.kwargs['random_id'])
    def test_unrelated_payload_is_ignored(self):
        event = self.event(); event['payload'] = {'kind': 'other'}
        with patch('quest_engine.quest_api') as save, patch.object(app, 'vote_feedback') as feedback:
            app.process_quest_vote_callback(event)
        save.assert_not_called(); feedback.assert_not_called()
    def test_wrong_group_callback_not_processed(self):
        with patch.object(app, 'VK_GROUP_ID', '17'), patch.object(app, 'process_quest_vote_callback') as process:
            app.process_vk_event(dict(type='message_event', group_id=18, object=self.event()))
        process.assert_not_called()
    def test_callback_route_accepts_message_event_and_acks_immediately(self):
        with patch.object(app, 'VK_GROUP_ID', '17'), patch.object(app.threading, 'Thread') as thread:
            result = app.app.test_client().post('/vk', json=dict(type='message_event', group_id=17, object=self.event()))
        self.assertEqual(result.data, b'ok')
        thread.assert_called_once(); thread.return_value.start.assert_called_once()
