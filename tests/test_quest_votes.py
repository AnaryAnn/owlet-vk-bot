import copy
import json
import unittest
from datetime import datetime, timezone
import native_quests
import quest_votes
from unittest.mock import patch

import app
from native_quests import quest_action
from quest_votes import HEADERS, version, options, keyboard, parse_payload
from test_native_features import QuestSheets


class Future(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2100, 1, 1, tzinfo=timezone.utc).astimezone(tz) if tz else cls(2100, 1, 1)


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
        result = quest_action(self.api, 'b', 'tick', dict(fact=0, **extra))
        if not extra.get('dryRun'):
            for item in result['outbox']:
                if item.get('keyboard'):
                    quest_action(self.api, 'b', 'ack', {'eventId': item['eventId']})
        return result
    def cast(self, **extra):
        payload = dict(questId='q', peerId='2000000002', vkId='123', choice=1, messageId=10)
        _, quests = native_quests.table(self.api, 'b', 'Квесты')
        _, stages = native_quests.table(self.api, 'b', 'Этапы')
        stage = stages[0]
        payload.update(stageId=stage['stage_id'], revision=version(quests[0], stage, options(stage, stages)))
        payload.update(extra)
        return quest_action(self.api, 'b', 'vote', payload)
    def close(self):
        with patch.object(native_quests, 'datetime', Future), patch.object(quest_votes, 'datetime', Future):
            return self.tick()
    def test_open_contains_choices_command_and_deadline(self):
        result = self.tick()
        text = result['outbox'][0]['text']
        self.assertIn('Нажми кнопку', text)
        self.assertEqual(result['outbox'][0]['keyboard']['buttons'][0][0]['action']['type'], 'text')
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
    def test_old_stage_and_changed_revision_buttons_are_rejected(self):
        self.tick()
        for data in ({'stageId': 'old'}, {'revision': '0'*16}):
            with self.assertRaisesRegex(ValueError, 'устарела'):
                self.cast(**data)
        self.assertEqual(len(self.api.sheets['Голоса']), 1)
    def test_expired_button_rejected_even_before_results_tick(self):
        self.tick()
        with patch.object(quest_votes, 'datetime', Future):
            with self.assertRaisesRegex(ValueError, 'завершён'):
                self.cast()
        self.assertEqual(len(self.api.sheets['Голоса']), 1)
    def test_changing_question_after_votes_stops_result(self):
        self.tick(); self.cast()
        self.api.sheets['Этапы'][1][3] = 'Изменённый вопрос'
        with self.assertRaisesRegex(ValueError, 'изменены'):
            self.close()
    def test_undelivered_vote_does_not_choose_empty_fallback(self):
        quest_action(self.api, 'b', 'tick', {'fact': 0})
        with patch.object(native_quests, 'datetime', Future), patch.object(quest_votes, 'datetime', Future):
            with self.assertRaisesRegex(RuntimeError, 'не было подтверждено'):
                quest_action(self.api, 'b', 'tick', {'fact': 0})
        self.assertEqual(self.api.sheets['Квесты'][1][2], 'running')
    def test_finished_vote_cannot_be_reopened(self):
        self.tick(); self.cast(); self.close()
        self.api.sheets['Квесты'][1][2:4] = ['active', 'start']
        with self.assertRaisesRegex(ValueError, 'уже завершён'):
            self.tick()
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
        self.assertIn('keyboard', result['outbox'][-1])


class VotingCallbackTests(unittest.TestCase):
    def test_vk_button_uses_identity_and_order_from_vk_event(self):
        selected = dict(kind='quest_vote', questId='q', stageId='s', revision='a'*16, choice=2,
                        vkId='999', peerId='2000000099')
        with app.app.app_context(), patch.object(app, 'VK_GROUP_ID', '17'), \
             patch('quest_engine.quest_api', return_value={'choice': 2, 'success': True}) as save, \
             patch.object(app, 'vk_send') as send:
            app.process_vk_event(dict(type='message_new', group_id=17, event_id='evt', object=dict(message=dict(
                from_id=123, peer_id=2000000002, text='2. Горы', payload=json.dumps(selected), conversation_message_id=99))))
        self.assertEqual(save.call_args.kwargs['messageId'], 99)
        self.assertEqual(save.call_args.kwargs['vkId'], '123')
        self.assertEqual(save.call_args.kwargs['peerId'], '2000000002')
        send.assert_called_once()
    def test_quest_job_without_book_skips_clearly(self):
        with patch.object(app, 'SCHEDULE_SECRET', 'test'), patch.dict('os.environ', {'QUEST_SPREADSHEET_ID': ''}):
            response = app.app.test_client().post('/native-schedule', json={'jobs': ['quests']},
                headers={'X-Schedule-Secret': 'test'})
        self.assertTrue(response.json['success'])
        self.assertTrue(response.json['jobs'][0]['skipped'])


class KeyboardTests(unittest.TestCase):
    def test_keyboard_payload_and_label_fit_limits(self):
        api = VotingSheets()
        _, quests = native_quests.table(api, 'b', 'Квесты')
        _, stages = native_quests.table(api, 'b', 'Этапы')
        choices = options(stages[0], stages)
        choices[0]['текст'] = 'Очень длинный вариант ' * 4
        result = keyboard(quests[0], stages[0], choices)
        self.assertTrue(result['inline'])
        self.assertFalse(result['one_time'])
        for row in result['buttons']:
            action = row[0]['action']
            self.assertLessEqual(len(action['label']), 40)
            self.assertLessEqual(len(action['payload'].encode()), 255)
            self.assertEqual(parse_payload(action['payload'])['stageId'], 'start')
    def test_unrelated_payload_ignored_invalid_vote_rejected(self):
        for payload in ('not json', '[]', '{"kind":"other"}', None):
            self.assertIsNone(parse_payload(payload))
        for value in (True, -1, 7, '1'):
            with self.assertRaises(ValueError):
                parse_payload(dict(kind='quest_vote', questId='q', stageId='s', choice=value, revision='a'*16))
    def test_vk_send_keyboard_only_on_first_chunk_and_stable_random_id(self):
        response = type('Response', (), {'raise_for_status': lambda _: None,
                                        'json': lambda _: {'response': 7}})()
        with patch.object(app, 'split_vk_message', return_value=['one', 'two']), \
             patch.object(app.requests, 'post', return_value=response) as post:
            app.vk_send(2000000002, 'text', keyboard={'inline': True, 'buttons': []}, random_id='quest:event')
            ids = [c.kwargs['data']['random_id'] for c in post.call_args_list]
            self.assertIn('keyboard', post.call_args_list[0].kwargs['data'])
            self.assertNotIn('keyboard', post.call_args_list[1].kwargs['data'])
            post.reset_mock()
            app.vk_send(2000000002, 'text', random_id='quest:event')
            self.assertEqual(ids, [c.kwargs['data']['random_id'] for c in post.call_args_list])
            self.assertNotEqual(ids[0], ids[1])
    def test_failed_keyboard_delivery_not_acknowledged(self):
        import quest_engine
        calls = []
        item = dict(eventId='q:s:vote_open', peerId='2000000002', text='Выбираем', keyboard={'buttons': []})
        def api(action, **kwargs):
            calls.append(action)
            return {'outbox': [item]}
        with patch.object(quest_engine, 'quest_api', side_effect=api):
            with self.assertRaises(RuntimeError):
                quest_engine.tick(lambda _: {'success': True},
                    lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError('offline')), 2000000002)
        self.assertNotIn('ack', calls)
