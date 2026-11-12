import copy
import json
import unittest
from unittest.mock import patch

import app
from native_quests import quest_action
from quest_votes import HEADERS
from test_native_features import QuestSheets


class VotingSheets(QuestSheets):
    def __init__(self):
        super().__init__()
        self.sheets['Голоса'] = [HEADERS]
        self.sheets['Этапы'][0] += ['варианты_JSON', 'ничья_этап']
        stage = self.sheets['Этапы'][1]
        stage[2] = 'vote'
        stage += [json.dumps([{'текст': 'Лес', 'этап': 'ok'}, {'текст': 'Горы', 'этап': 'fail'}]), '']
    def ensure_tab(self, book, name, headers):
        return self.tabs(book)[name]['sheetId']


class VotingTests(unittest.TestCase):
    def setUp(self):
        self.api = VotingSheets()
    def tick(self, **extra):
        return quest_action(self.api, 'b', 'tick', dict(fact=0, **extra))
    def vote(self, **extra):
        return quest_action(self.api, 'b', 'vote', dict(questId='q', peerId='2000000002',
            vkId='123', choice=1, messageId=10, **extra))
    def cast(self, **extra):
        payload = dict(questId='q', peerId='2000000002', vkId='123', choice=1, messageId=10)
        payload.update(extra)
        return quest_action(self.api, 'b', 'vote', payload)
    def close(self):
        self.api.sheets['Этапы'][1][6] = '2000-01-01T00:00:00Z'
        return self.tick()
    def test_open_contains_choices_command_and_deadline(self):
        result = self.tick()
        text = result['outbox'][0]['text']
        self.assertIn('голос q 1', text)
        self.assertIn('МСК', text)
        self.assertEqual(self.api.sheets['Квесты'][1][2], 'running')
        self.assertEqual(self.tick()['transitions'], [])
    def test_last_vote_wins_and_stale_callback_cannot_revert(self):
        self.tick()
        self.cast()
        self.cast(choice=2, messageId=12)
        self.assertTrue(self.cast(choice=1, messageId=10)['duplicate'])
        self.assertEqual(len(self.api.sheets['Голоса']), 2)
        result = self.close()
        self.assertEqual(result['transitions'][0]['to'], 'fail')
        self.assertIn('Горы: 1', result['outbox'][-1]['text'])
        self.assertEqual(self.tick()['transitions'], [])
    def test_wrong_peer_inactive_and_invalid_choice_rejected(self):
        with self.assertRaises(ValueError): self.cast()
        self.tick()
        for data in ({'peerId': '2000000009'}, {'choice': 8}, {'vkId': '-1'}):
            with self.assertRaises(ValueError): self.cast(**data)
        self.assertEqual(len(self.api.sheets['Голоса']), 1)
    def test_late_vote_rejected_and_empty_uses_fallback(self):
        self.tick()
        self.close()
        with self.assertRaises(ValueError): self.cast()
        self.assertEqual(self.api.sheets['Квесты'][1][3], 'fail')
    def test_tie_first_and_explicit_tie_branch(self):
        for tie, expected in [('', 'ok'), ('fail', 'fail')]:
            self.api = VotingSheets()
            self.api.sheets['Этапы'][1][-1] = tie
            self.tick(); self.cast(); self.cast(vkId='456', choice=2)
            self.assertEqual(self.close()['transitions'][0]['to'], expected)
    def test_vote_result_can_continue_nonterminal_stage(self):
        self.api.sheets['Этапы'][2][2] = 'task'
        self.tick(); self.cast(); self.close()
        self.assertEqual(self.api.sheets['Квесты'][1][2:4], ['running', 'ok'])
    def test_dry_run_leaves_sheets_untouched(self):
        before = copy.deepcopy(self.api.sheets)
        self.tick(dryRun=True)
        self.assertEqual(self.api.sheets, before)
    def test_bad_options_rejected_before_any_writes(self):
        self.api.sheets['Этапы'][1][-2] = '[{"текст":"Лес","этап":"missing"}]'
        with self.assertRaises(ValueError): self.tick()
        self.assertEqual(self.api.writes, [])
    def test_threshold_stage_opens_vote_and_stays_running(self):
        stage = self.api.sheets['Этапы'][1]
        stage[1] = 'voting'
        self.api.sheets['Этапы'].append(['q', 'start', 'start', 'Начало', '0', '',
            '2099-01-01T00:00:00Z', 'voting', 'fail', '', ''])
        self.tick()
        result = self.tick()
        self.assertEqual(self.api.sheets['Квесты'][1][2:4], ['running', 'voting'])
        self.assertIn('голос q 1', result['outbox'][-1]['text'])


class VotingCallbackTests(unittest.TestCase):
    def test_vk_message_passes_identity_and_message_order_to_storage(self):
        with app.app.app_context(), patch.object(app, 'VK_GROUP_ID', '17'), \
             patch.object(app, 'command_event_once', return_value=True), \
             patch('quest_engine.quest_api', return_value={'choice': 2, 'success': True}) as save, \
             patch.object(app, 'vk_send') as send:
            app.process_vk_event(dict(type='message_new', group_id=17, event_id='evt', object=dict(message=dict(
                from_id=123, peer_id=2000000002, text='голос q 2', conversation_message_id=99))))
        self.assertEqual(save.call_args.kwargs['messageId'], 99)
        self.assertEqual(save.call_args.kwargs['vkId'], '123')
        self.assertEqual(save.call_args.kwargs['peerId'], '2000000002')
        send.assert_called_once()
    def test_quest_job_is_explicitly_supported_and_no_book_skips_clearly(self):
        with patch.object(app, 'SCHEDULE_SECRET', 'test'), patch.dict('os.environ', {'QUEST_SPREADSHEET_ID': ''}):
            response = app.app.test_client().post('/native-schedule', json={'jobs': ['quests']},
                headers={'X-Schedule-Secret': 'test'})
        self.assertTrue(response.json['success'])
        self.assertTrue(response.json['jobs'][0]['skipped'])
        self.assertIn('QUEST_SPREADSHEET_ID', response.json['jobs'][0]['reason'])
