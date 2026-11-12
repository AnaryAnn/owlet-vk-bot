import copy
import json
import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import app
import native_quests
import quest_polls
from native_quests import quest_action
from quest_polls import HEADERS, fingerprint, publish, refresh, recover, VKRejected
from quest_votes import options, summarize
from test_native_features import QuestSheets


class Future(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2100, 1, 1, tzinfo=timezone.utc).astimezone(tz) if tz else cls(2100, 1, 1)


class VotingSheets(QuestSheets):
    def __init__(self):
        super().__init__()
        self.sheets['_QuestPolls'] = [HEADERS]
        self.sheets['Этапы'][0] += ['варианты_JSON', 'ничья_этап']
        stage = self.sheets['Этапы'][1]
        stage[2] = 'vote'
        stage += [json.dumps([{'текст': 'Лес', 'этап': 'ok'}, {'текст': 'Горы', 'этап': 'fail'}]), '']
    def ensure_tab(self, book, name, headers):
        self.sheets.setdefault(name, [headers])
        return self.tabs(book)[name]['sheetId']


class PollStateTests(unittest.TestCase):
    def setUp(self):
        self.api = VotingSheets()
    def call(self, action, **kwargs):
        return quest_action(self.api, 'b', action, kwargs)
    def tick(self, **extra):
        return self.call('tick', fact=0, **extra)
    def claim(self):
        return self.call('pollClaim', questId='q', stageId='start', eventId='q:start:vote_open')
    def bind(self):
        return self.call('pollBind', questId='q', stageId='start', ownerId=12, pollId=99, answerIds=[41, 42])
    def published(self):
        return self.call('pollPublished', questId='q', stageId='start', messageId=7)
    def prepare(self):
        self.tick(); self.claim(); self.bind(); self.published()
    def result(self, counts):
        with patch.object(native_quests, 'datetime', Future), patch.object(quest_polls, 'datetime', Future):
            self.call('pollSnapshot', questId='q', stageId='start', counts=counts, closed=True)
            return self.tick()
    def test_open_native_poll_not_chat_commands(self):
        result = self.tick()
        self.assertEqual(result['outbox'][0]['poll'], {'questId': 'q', 'stageId': 'start'})
        self.assertIn('прикреплённом опросе', result['outbox'][0]['text'])
        self.assertNotIn('голос q', result['outbox'][0]['text'])
    def test_uncertain_creation_not_repeated(self):
        self.tick(); self.assertTrue(self.claim()['create'])
        with self.assertRaisesRegex(RuntimeError, 'не подтверждено'): self.claim()
        self.assertEqual(len(self.api.sheets['_QuestPolls']), 2)
    def test_explicit_api_rejection_allows_retry(self):
        self.tick(); self.claim()
        self.call('pollCreateFailed', questId='q', stageId='start')
        self.assertTrue(self.claim()['create'])
    def test_bound_poll_reused(self):
        self.tick(); self.claim(); self.bind()
        self.assertEqual(self.claim()['poll'], {'ownerId': 12, 'pollId': 99})
    def test_no_vk_snapshot_no_branch_selected(self):
        self.prepare()
        with patch.object(native_quests, 'datetime', Future):
            self.assertEqual(self.tick()['transitions'], [])
        self.assertEqual(self.api.sheets['Квесты'][1][2], 'running')
    def test_winner_and_no_duplicate_transition(self):
        self.prepare()
        result = self.result([1, 5])
        self.assertEqual(result['transitions'][0]['to'], 'fail')
        self.assertIn('Горы: 5', result['outbox'][-1]['text'])
        self.assertNotIn('poll', result['outbox'][-1])
        self.assertEqual(self.tick()['transitions'], [])
    def test_empty_and_tie_rules(self):
        stage = {'провал_этап': 'empty', 'ничья_этап': 'tie'}
        choices = [{'текст': 'Лес', 'этап': 'forest'}, {'текст': 'Горы', 'этап': 'mountains'}]
        self.assertEqual(summarize(stage, choices, [0, 0])[0], 'empty')
        self.assertEqual(summarize(stage, choices, [4, 4])[0], 'tie')
        stage['ничья_этап'] = ''
        self.assertEqual(summarize(stage, choices, [4, 4])[0], 'forest')
    def test_snapshot_rejects_early_unclosed_or_negative_results(self):
        self.prepare()
        with self.assertRaises(ValueError):
            self.call('pollSnapshot', questId='q', stageId='start', counts=[1, 2], closed=True)
        with patch.object(quest_polls, 'datetime', Future):
            for data in ({'counts': [1, 2], 'closed': False}, {'counts': [-1, 2], 'closed': True}):
                with self.assertRaises(ValueError):
                    self.call('pollSnapshot', questId='q', stageId='start', **data)
    def test_changed_configuration_does_not_reuse_poll(self):
        self.prepare()
        self.api.sheets['Этапы'][1][3] = 'Другой вопрос'
        with self.assertRaisesRegex(ValueError, 'изменены'): self.claim()
    def test_dry_run_does_not_create_poll_state(self):
        before = copy.deepcopy(self.api.sheets)
        self.tick(dryRun=True)
        self.assertEqual(self.api.sheets, before)
    def test_nonterminal_branch_stays_running(self):
        self.api.sheets['Этапы'][2][2] = 'task'
        self.prepare(); self.result([5, 1])
        self.assertEqual(self.api.sheets['Квесты'][1][2:4], ['running', 'ok'])
    def test_threshold_opens_vote_instead_of_finishing(self):
        self.api.sheets['Этапы'][1][1] = 'voting'
        self.api.sheets['Этапы'].append(['q', 'start', 'start', 'Начало', '0', '',
            '2099-01-01T00:00:00Z', 'voting', 'fail', '', ''])
        self.tick(); result = self.tick()
        self.assertEqual(self.api.sheets['Квесты'][1][2:4], ['running', 'voting'])
        self.assertEqual(result['outbox'][-1]['poll']['stageId'], 'voting')


class PollTransportTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {'VK_POLL_USER_TOKEN': 'private-user-token', 'VK_TOKEN': 'group-token'})
        self.env.start(); self.addCleanup(self.env.stop)
        self.item = dict(eventId='q:start:vote_open', peerId='2000000002', text='Выбираем путь',
                         poll=dict(questId='q', stageId='start'))
        self.config = dict(success=True, create=True, question='Куда?', answers=['Лес', 'Горы'], endDate=100000)
        self.poll = dict(id=99, owner_id=12, question='Куда?', multiple=False, end_date=100000,
                         closed=True, answers=[dict(id=41, text='Лес', votes=3), dict(id=42, text='Горы', votes=2)])
        self.message = {'items': [dict(id=7, peer_id=2000000002,
            attachments=[dict(type='poll', poll=dict(id=99, owner_id=12))])]}
    def api(self, action, **kwargs):
        self.calls.append((action, kwargs))
        return self.config if action == 'pollClaim' else {'success': True}
    def test_create_send_verify_and_mark_published(self):
        self.calls = []
        with patch.object(quest_polls, 'vk_call', side_effect=[self.poll, 7, self.message]) as vk:
            publish(self.item, self.api)
        self.assertEqual([c[0] for c in self.calls], ['pollClaim', 'pollBind', 'pollPublished'])
        self.assertEqual(vk.call_args_list[0].args[:2], ('polls.create', 'private-user-token'))
        self.assertEqual(vk.call_args_list[1].args[:2], ('messages.send', 'group-token'))
        self.assertEqual(vk.call_args_list[1].kwargs['attachment'], 'poll12_99')
    def test_retry_reuses_poll_and_same_message_random_id(self):
        self.config = dict(poll=dict(ownerId=12, pollId=99))
        self.calls = []
        with patch.object(quest_polls, 'vk_call', side_effect=[7, self.message, 7, self.message]) as vk:
            publish(self.item, self.api); publish(self.item, self.api)
        self.assertEqual(vk.call_args_list[0].kwargs['random_id'], vk.call_args_list[2].kwargs['random_id'])
        self.assertFalse(any(c[0] == 'pollBind' for c in self.calls))
    def test_missing_attachment_never_marks_published(self):
        self.calls = []
        with patch.object(quest_polls, 'vk_call', side_effect=[self.poll, 7, {'items': []}]):
            with self.assertRaisesRegex(RuntimeError, 'вложение'): publish(self.item, self.api)
        self.assertNotIn('pollPublished', [c[0] for c in self.calls])
    def test_missing_token_does_not_claim(self):
        self.calls = []
        with patch.dict(os.environ, {'VK_POLL_USER_TOKEN': ''}):
            with self.assertRaises(RuntimeError): publish(self.item, self.api)
        self.assertEqual(self.calls, [])
    def test_timeout_does_not_release_creation_but_explicit_rejection_does(self):
        for error, released in [(RuntimeError('timeout'), False), (VKRejected('access denied'), True)]:
            self.calls = []
            with patch.object(quest_polls, 'vk_call', side_effect=error):
                with self.assertRaises(RuntimeError): publish(self.item, self.api)
            self.assertEqual('pollCreateFailed' in [c[0] for c in self.calls], released)
    def test_results_map_by_answer_id_not_api_order(self):
        calls = []
        def api(action, **kw):
            calls.append((action, kw))
            return dict(tasks=[dict(questId='q', stageId='start', ownerId=12, pollId=99,
                answerIds=[41, 42], endDate=100000, question='Куда?', answerTexts=['Лес', 'Горы'])])
        self.poll['answers'].reverse()
        with patch.object(quest_polls, 'vk_call', return_value=self.poll): refresh(2000000002, api)
        self.assertEqual(calls[-1][1]['counts'], [3, 2])
    def test_changed_or_unclosed_poll_never_applies_result(self):
        for field, value in [('closed', False), ('end_date', 999), ('multiple', True), ('question', 'Другое?')]:
            poll = dict(self.poll, **{field: value})
            calls = []
            def api(action, **kw):
                calls.append(action)
                return dict(tasks=[dict(questId='q', stageId='start', ownerId=12, pollId=99,
                    answerIds=[41, 42], endDate=100000, question='Куда?', answerTexts=['Лес', 'Горы'])])
            with patch.object(quest_polls, 'vk_call', return_value=poll):
                with self.assertRaises((RuntimeError, ValueError)): refresh(2000000002, api)
            self.assertEqual(calls, ['pollTasks'])
    def test_api_errors_do_not_expose_token(self):
        import requests
        with patch.object(quest_polls.requests, 'post', side_effect=requests.Timeout('private-user-token')):
            with self.assertRaises(RuntimeError) as error: quest_polls.vk_call('polls.create', 'private-user-token')
        self.assertNotIn('private-user-token', str(error.exception))
    def test_recovery_route_requires_secret(self):
        with patch.object(app, 'SCHEDULE_SECRET', 'test'):
            self.assertEqual(app.app.test_client().post('/quest-poll-recover', json={}).status_code, 403)

    def test_recovery_verifies_existing_poll_before_binding(self):
        calls = []
        def api(action, **kw):
            calls.append((action, kw))
            return self.config if action == 'pollConfig' else {'success': True}
        with patch.object(quest_polls, 'vk_call', return_value=self.poll):
            recover(dict(questId='q', stageId='start', ownerId=12, pollId=99), api)
        self.assertEqual([a for a, _ in calls], ['pollConfig', 'pollBind'])
        self.assertEqual(calls[-1][1]['answerIds'], [41, 42])
    def test_recovery_wrong_question_never_binds(self):
        calls = []
        def api(action, **kw):
            calls.append(action)
            return self.config
        poll = dict(self.poll, question='Подмена?')
        with patch.object(quest_polls, 'vk_call', return_value=poll):
            with self.assertRaises(ValueError):
                recover(dict(questId='q', stageId='start', ownerId=12, pollId=99), api)
        self.assertEqual(calls, ['pollConfig'])


class PollEngineTests(unittest.TestCase):
    def test_failed_poll_delivery_is_not_acknowledged(self):
        import quest_engine
        item = dict(eventId='q:s:vote_open', peerId='2000000002', text='Вопрос',
                    poll=dict(questId='q', stageId='s'))
        calls = []
        def api(action, **kw):
            calls.append(action)
            return {'tasks': []} if action == 'pollTasks' else {'outbox': [item]}
        with patch.object(quest_engine, 'quest_api', side_effect=api), \
             patch.object(quest_polls, 'publish', side_effect=RuntimeError('Не доставлен')):
            with self.assertRaises(RuntimeError):
                quest_engine.tick(lambda _: {'success': True}, lambda *_: None, 2000000002)
        self.assertNotIn('ack', calls)
    def test_dry_run_does_not_contact_vk(self):
        import quest_engine
        with patch.object(quest_engine, 'quest_api', return_value={'transitions': []}), \
             patch.object(quest_polls, 'vk_call') as vk:
            quest_engine.tick(lambda _: {'success': True}, lambda *_: None, 2000000002, dry_run=True)
        vk.assert_not_called()
