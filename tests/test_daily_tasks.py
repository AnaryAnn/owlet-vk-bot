import json
import time
import unittest
from unittest.mock import Mock, patch

import app
import daily_tasks as daily
import website_store as store
from test_website_store import MemorySheets


class DailyTaskTests(unittest.TestCase):
    def setUp(self):
        self.api = MemorySheets()
        self.now = time.time()
        clock_patch = patch.object(daily.time, 'time', return_value=self.now)
        self.clock = clock_patch.start(); self.addCleanup(clock_patch.stop)
    def get_task(self, level='easy', target=2):
        menu = daily.request(self.api, '123')
        payload = json.loads(menu['keyboard']['buttons'][0][0]['action']['payload'])
        result = daily.request(self.api, '123', level=level, offer=payload['offer'], rng=Mock(randint=Mock(return_value=target)))
        return result['task'], payload['offer']
    def report(self, fact, key='r1', plan=30):
        return store.save_report(self.api, 'Тест', plan, fact, key)[0]
    def current(self):
        return daily.state(self.api, '123')[1]
    def test_three_levels_and_baseline_from_fact_not_plan(self):
        task, _ = self.get_task('normal', 5)
        self.assertEqual(task['baseline_fact'], 10)
        self.assertEqual(task['deadline'] - task['issued'], 86400)
        self.assertEqual(self.report(14)['dailyTask']['progress'], 4)
        self.assertEqual(self.report(15, 'r2')['dailyTask']['status'], 'completed')
        self.assertEqual(int(self.current()[2]), 3)
    def test_repeat_report_cannot_award_points_twice(self):
        self.get_task('heroic', 8)
        first = self.report(18)
        before = len(self.api.batches)
        self.assertEqual(self.report(18), first)
        self.assertEqual(len(self.api.batches), before)
        self.assertEqual(len(daily.rows(self.api, '_DailyRewards')), 1)
        self.assertEqual(int(self.current()[2]), 6)
    def test_plan_changes_and_lower_fact_do_not_complete_task(self):
        self.get_task('easy', 3)
        self.assertEqual(self.report(10, plan=100)['dailyTask']['progress'], 0)
        self.assertEqual(self.report(8, 'r2')['dailyTask']['progress'], 0)
        self.assertEqual(self.report(12, 'r3')['dailyTask']['progress'], 2)
        self.assertEqual(self.report(12, 'r4')['dailyTask']['progress'], 2)
    def test_active_task_cannot_be_changed_or_extended(self):
        task, offer = self.get_task('easy', 1)
        self.clock.return_value = self.now + 100
        again = daily.request(self.api, '123', level='heroic', offer=offer)
        self.assertEqual(again['task'], task)
    def test_old_button_cannot_issue_new_task_after_completion(self):
        task, offer = self.get_task('easy', 1)
        self.report(11)
        with self.assertRaisesRegex(ValueError, 'устарела'):
            daily.request(self.api, '123', level='heroic', offer=offer)
    def test_expiry_preserves_points_and_allows_new_task(self):
        self.get_task('easy', 1); self.report(11)
        task, _ = self.get_task('heroic', 8)
        self.clock.return_value = task['deadline']
        result = self.report(100, 'late')
        self.assertEqual(result['dailyTask']['status'], 'expired')
        self.assertEqual(int(self.current()[2]), 2)
        next_task, _ = self.get_task('normal', 4)
        self.assertNotEqual(task['id'], next_task['id'])
        self.assertEqual(next_task['baseline_fact'], 100)
    def test_mixed_levels_award_every_six_points_keep_remainder(self):
        for n, (level, target, fact) in enumerate([('easy', 1, 11), ('normal', 4, 15), ('easy', 1, 16)]):
            self.get_task(level, target)
            self.report(fact, 'r'+str(n))
        self.assertEqual(int(self.current()[2]), 7)
        self.assertEqual(daily.rows(self.api, '_DailyRewards')[0][0], 'daily:123:1')
        self.assertEqual(len(daily.rows(self.api, '_DailyRewards')), 1)
    def test_week_reset_preserves_previously_reported_progress(self):
        self.get_task('normal', 5)
        self.report(12)
        writes, status = daily.report_update(self.api, 'Тест', '123', {'start': 'new-week'}, 3)
        self.api.batch(store.SHEET_ID, writes)
        self.assertEqual(status['progress'], 5)
        self.assertEqual(status['status'], 'completed')
    def test_report_and_task_completion_share_atomic_batch(self):
        self.get_task('heroic', 8)
        self.report(18)
        batch = self.api.batches[-1]
        ids = {w.get('appendCells', {}).get('sheetId') or w.get('updateCells', {}).get('start', {}).get('sheetId') for w in batch}
        tabs = self.api.tabs(store.SHEET_ID)
        self.assertIn(tabs['_DailyTasks']['sheetId'], ids)
        self.assertIn(tabs['_DailyRewards']['sheetId'], ids)
        self.assertIn(tabs['_WebsiteReports']['sheetId'], ids)
    def test_unknown_user_not_registered_or_given_task(self):
        with self.assertRaises(ValueError): daily.request(self.api, '999')
        self.assertNotIn('_DailyTasks', self.api.tabs(store.SHEET_ID))
    def test_duplicate_menu_requests_keep_same_offer(self):
        first = daily.request(self.api, '123')
        second = daily.request(self.api, '123')
        self.assertEqual(first['keyboard'], second['keyboard'])
    def test_guaranteed_drop_with_only_common_catalog_and_idempotence(self):
        report = dict(requestId='daily:123:1', vkId='123', name='Тест', plan=0, fact=0)
        rng = Mock(); rng.random.return_value = .99; rng.choice.side_effect = lambda a: a[0]
        result = store.artifact_drop(self.api, report, rng=rng, guaranteed=True)
        self.assertTrue(result['dropped']); self.assertEqual(result['chance'], 1)
        same = store.artifact_drop(self.api, report, rng=rng, guaranteed=True)
        self.assertEqual(result, same)
        self.assertEqual(self.api.books[store.ARTIFACTS_ID]['Инвентарь'][1][5], 1)
    def test_empty_catalog_does_not_mark_guaranteed_reward_consumed(self):
        self.api.books[store.ARTIFACTS_ID]['Артефакты'] = [['ID', 'Редкость', 'Имя']]
        with self.assertRaises(ValueError):
            store.artifact_drop(self.api, dict(requestId='daily:123:1', vkId='123', name='Тест', plan=0, fact=0), guaranteed=True)
        self.assertEqual(daily.rows(self.api, '_DailyRewards') if '_DailyRewards' in self.api.tabs(store.SHEET_ID) else [], [])
        self.assertEqual(self.api.books[store.ARTIFACTS_ID]['_ArtifactDrops'][1:], [])
    def test_reward_worker_only_once_and_sends_private_notifications(self):
        self.get_task('heroic', 8); self.report(18)
        sender = Mock()
        with patch.dict('os.environ', {'NATIVE_STORAGE_ENABLED': 'true'}), \
             patch.object(daily, '_session'), patch.object(daily, 'Sheets', return_value=self.api):
            daily.process_pending('123', sender=sender)
            count = sender.call_count
            daily.process_pending('123', sender=sender)
        self.assertEqual(sender.call_count, count)
        self.assertEqual(self.api.books[store.ARTIFACTS_ID]['Инвентарь'][1][5], 1)
        self.assertTrue(all(c.args[0] == 123 for c in sender.call_args_list))
        self.assertEqual(daily.rows(self.api, '_DailyRewards')[0][3], 'done')
    def test_issuance_random_target_once_and_within_selected_range(self):
        for level, (_, low, high, points) in daily.LEVELS.items():
            self.api = MemorySheets()
            rng = Mock(); rng.randint.return_value = high
            menu = daily.request(self.api, '123')
            offer = json.loads(menu['keyboard']['buttons'][0][0]['action']['payload'])['offer']
            task = daily.request(self.api, '123', level, offer, rng=rng)['task']
            daily.request(self.api, '123', level, offer, rng=rng)
            rng.randint.assert_called_once_with(low, high)
            self.assertEqual(task['target'], high)


class DailyRoutingTests(unittest.TestCase):
    def test_command_case_insensitive_private_only(self):
        with app.app.app_context(), patch.object(app, 'VK_GROUP_ID', '17'), \
             patch('daily_tasks.transact', return_value={'text': 'Выбери', 'keyboard': {'buttons': []}}) as action, \
             patch('daily_tasks.safe_pending'), patch.object(app, 'vk_send') as send:
            app.process_vk_event(dict(type='message_new', group_id=17, event_id='e', object=dict(message=dict(
                peer_id=123, from_id=123, text='  ЗАДАНИЕ  '))))
        action.assert_called_once(); self.assertEqual(send.call_args.args[0], 123)
    def test_daily_callback_cannot_be_used_from_group_chat(self):
        event = dict(user_id=123, peer_id=2000000002, event_id='e', payload=dict(kind='daily_task', level='easy', offer='a'*32))
        with patch('daily_tasks.transact') as action:
            app.process_daily_callback(event)
        action.assert_not_called()


class DailyQueueFailureTests(unittest.TestCase):
    def test_failed_award_keeps_credit_and_queue_for_retry(self):
        api = MemorySheets()
        m = daily.request(api, '123')
        offer = json.loads(m['keyboard']['buttons'][0][0]['action']['payload'])['offer']
        daily.request(api, '123', 'heroic', offer, rng=Mock(randint=Mock(return_value=8)))
        store.save_report(api, 'Тест', 30, 18, 'report')
        with patch.dict('os.environ', {'NATIVE_STORAGE_ENABLED': 'true'}), \
             patch.object(daily, '_session'), patch.object(daily, 'Sheets', return_value=api), \
             patch.object(daily, 'artifact_drop', side_effect=RuntimeError('temporarily unavailable')):
            daily.process_pending('123', sender=Mock())
        self.assertEqual(daily.rows(api, '_DailyRewards')[0][3], 'pending')
        self.assertEqual(int(daily.state(api, '123')[1][2]), 6)
        with patch.dict('os.environ', {'NATIVE_STORAGE_ENABLED': 'true'}), \
             patch.object(daily, '_session'), patch.object(daily, 'Sheets', return_value=api):
            daily.process_pending('123', sender=Mock())
        self.assertEqual(daily.rows(api, '_DailyRewards')[0][3], 'done')
        self.assertEqual(api.books[store.ARTIFACTS_ID]['Инвентарь'][1][5], 1)
    def test_failed_private_message_stays_pending_with_stable_key(self):
        api = MemorySheets()
        m = daily.request(api, '123')
        offer = json.loads(m['keyboard']['buttons'][0][0]['action']['payload'])['offer']
        daily.request(api, '123', 'easy', offer, rng=Mock(randint=Mock(return_value=1)))
        store.save_report(api, 'Тест', 30, 11, 'report')
        failing = Mock(side_effect=RuntimeError('VK unavailable')); succeeding = Mock()
        with patch.dict('os.environ', {'NATIVE_STORAGE_ENABLED': 'true'}), \
             patch.object(daily, '_session'), patch.object(daily, 'Sheets', return_value=api):
            daily.process_pending('123', sender=failing)
            self.assertEqual(daily.rows(api, '_DailyNotices')[0][4], 'pending')
            daily.process_pending('123', sender=succeeding)
        self.assertEqual(failing.call_args.kwargs['random_id'], succeeding.call_args.kwargs['random_id'])
        self.assertEqual(daily.rows(api, '_DailyNotices')[0][4], 'sent')


class ReportEntryPointTests(unittest.TestCase):
    def setup_task(self):
        api = MemorySheets()
        result = daily.request(api, '123')
        offer = json.loads(result['keyboard']['buttons'][0][0]['action']['payload'])['offer']
        daily.request(api, '123', 'easy', offer, rng=Mock(randint=Mock(return_value=1)))
        return api
    def test_vk_report_updates_task_and_schedules_private_queue(self):
        import native_storage
        api = self.setup_task()
        with patch.dict('os.environ', {'NATIVE_STORAGE_ENABLED': 'true'}), \
             patch.object(native_storage, '_session'), patch.object(native_storage, 'Sheets', return_value=api), \
             patch.object(native_storage, 'stats_get', return_value={'name': 'Тест'}), \
             patch.object(daily, 'safe_pending') as pending:
            result = native_storage.stats_post(dict(action='vkSave', vkId='123', plan=30, fact=11, reportId='vk-report'))
        self.assertEqual(result['dailyTask']['status'], 'completed')
        pending.assert_called_once_with('123')
    def test_website_report_updates_task_and_schedules_private_queue(self):
        api = self.setup_task()
        with patch.dict('os.environ', {'NATIVE_STORAGE_ENABLED': 'true'}), \
             patch.object(store, '_session'), patch.object(store, 'Sheets', return_value=api), \
             patch.object(store, 'artifact_drop', return_value={'dropped': False}), \
             patch.object(store, 'send_notification'), patch.object(daily, 'safe_pending') as pending:
            result = store.submit_report(dict(name='Тест', plan=30, fact=11, requestId='abcdef0123456789'))
        self.assertEqual(result['dailyTask']['status'], 'completed')
        pending.assert_called_once_with('123')
