import base64
import copy
import json
import threading
import unittest
from contextlib import contextmanager
from unittest.mock import Mock, patch

import requests
from flask import Flask
import website_api
import website_store as store


class MemorySheets:
    def __init__(self):
        self.uncertain = False
        week = store.current_week()['sheetName']
        self.books = {
            store.SHEET_ID: {
                'Участники': [['Имя', 'VK ID', 'Статус'], ['Тест', '123', 'активен']],
                week: [[''] for _ in range(7)] + [['Тест', 30, 10, 'вчера']]},
            store.ARTIFACTS_ID: {
                'Артефакты': [['ID', 'Редкость', 'Имя', 'Описание', 'Фото'],
                    ['1', 'обычный', 'Перо', 'Описание', 'photo-1_2']],
                'Инвентарь': [['VK ID', 'Имя', 'ID', 'Название', 'Редкость', 'Количество', 'Первый', 'Последний']]}}
        self.ids = {}; self.locks = {}; self.fail = False; self.batches = []

    @contextmanager
    def locked(self, book):
        lock = self.locks.setdefault(book, threading.Lock())
        with lock:
            yield

    def tabs(self, book):
        result = {}
        for title in self.books[book]:
            key = (book, title)
            if key not in self.ids:
                self.ids[key] = len(self.ids) + 1
            result[title] = {'sheetId': self.ids[key]}
        return result

    def ensure_tab(self, book, title, headers, hidden=True):
        self.books[book].setdefault(title, [headers])
        return self.tabs(book)[title]['sheetId']

    def values(self, book, ranges, formatted=False):
        result = []
        for r in ranges:
            title, area = r.rsplit('!', 1)
            rows = self.books[book][title.strip("'")]
            if area == 'B2': values = [rows[1][1:2]] if len(rows) > 1 else []
            elif area.startswith('A8'): values = rows[7:]
            elif area == 'A:Z': values = rows
            elif area.startswith('C'):
                index = int(area[1:]) - 1; values = [rows[index][2:3]]
            else: values = rows[1:]
            result.append(copy.deepcopy(values))
        return result

    def batch(self, book, writes):
        if self.fail:
            raise requests.Timeout('тест')
        self.batches.append(copy.deepcopy(writes))
        for write in writes:
            if 'addSheet' in write:
                properties = write['addSheet']['properties']
                self.books[book][properties['title']] = []
                self.ids[(book, properties['title'])] = properties['sheetId']
                continue
            obj = write.get('updateCells') or write.get('appendCells')
            sid = obj.get('sheetId') or obj['start']['sheetId']
            title = next(t for (b, t), i in self.ids.items() if b == book and i == sid)
            rows = self.books[book][title]
            for offset, rowdata in enumerate(obj['rows']):
                values = [next(iter(c['userEnteredValue'].values())) for c in rowdata['values']]
                if 'appendCells' in write:
                    rows.append(values)
                else:
                    row, col = obj['start']['rowIndex'] + offset, obj['start']['columnIndex']
                    while len(rows) <= row: rows.append([])
                    rows[row].extend([''] * max(0, col + len(values) - len(rows[row])))
                    rows[row][col:col + len(values)] = values


def report_payload(**kwargs):
    return dict(name='Тест', plan=30, fact=17, requestId='abcdef0123456789', **kwargs)


class RecordTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict('os.environ', {'NATIVE_STORAGE_ENABLED': 'true'})
        env.start(); self.addCleanup(env.stop)
        self.api = MemorySheets()
        self.args = store.payload_values(report_payload())

    def test_validation(self):
        for key, value in [('plan', True), ('fact', float('nan')), ('fact', 1.2),
                           ('fact', -1), ('plan', '30'), ('requestId', 'short'), ('name', '')]:
            data = report_payload(); data[key] = value
            with self.assertRaises(ValueError): store.payload_values(data)

    def test_atomic_record_and_journal_and_retry(self):
        saved, _, _ = store.save_report(self.api, *self.args)
        self.assertTrue(saved['artifactEligible'])
        self.assertEqual(saved['previousFact'], 10)
        self.assertEqual(len(self.api.batches[0]), 2)
        self.assertIn('updateCells', self.api.batches[0][0])
        self.assertIn('appendCells', self.api.batches[0][1])
        again, _, _ = store.save_report(self.api, *self.args)
        self.assertEqual(again, saved)
        self.assertEqual(len(self.api.batches), 1)

    def test_conflicting_id(self):
        store.save_report(self.api, *self.args)
        args = list(self.args); args[2] = 18
        with self.assertRaises(store.Conflict): store.save_report(self.api, *args)

    def test_same_fact_no_roll_and_unchanged_no_message(self):
        args = list(self.args); args[2] = 10
        result, _, _ = store.save_report(self.api, *args)
        self.assertFalse(result['artifactEligible'])
        self.assertFalse(result['changed'])
        self.assertEqual(result['notificationStatus'], 'skipped')

    def test_removed_or_ambiguous_participant(self):
        self.api.books[store.SHEET_ID]['Участники'].append(['Тест', '456', 'активен'])
        with self.assertRaises(ValueError): store.save_report(self.api, *self.args)
        self.assertFalse(self.api.batches)

    def test_two_channels_only_increased_fact_is_eligible(self):
        store.save_report(self.api, *self.args)
        args = list(self.args); args[-1] = 'website-newrequest123456'
        result, _, _ = store.save_report(self.api, *args)
        self.assertFalse(result['artifactEligible'])

    def test_concurrent_same_request(self):
        results = []
        threads = [threading.Thread(target=lambda: results.append(store.save_report(self.api, *self.args))) for _ in range(2)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(len(results), 2)
        self.assertEqual(len(self.api.batches), 1)

    def test_artifact_atomic_and_idempotent(self):
        report, _, _ = store.save_report(self.api, *self.args)
        rng = Mock(); rng.random.side_effect = [0, 0]; rng.choice.side_effect = lambda a: a[0]
        result = store.artifact_drop(self.api, report, rng)
        self.assertTrue(result['dropped'])
        self.assertEqual(len(self.api.batches[-1]), 2)
        again = store.artifact_drop(self.api, report, rng)
        self.assertEqual(result, again)
        self.assertEqual(len(self.api.books[store.ARTIFACTS_ID]['Инвентарь']), 2)

    def test_artifact_miss_logged(self):
        report, _, _ = store.save_report(self.api, *self.args)
        rng = Mock(); rng.random.return_value = .99
        result = store.artifact_drop(self.api, report, rng)
        self.assertFalse(result['dropped'])
        self.assertEqual(result['chance'], .1)
        self.assertEqual(len(self.api.books[store.ARTIFACTS_ID]['_ArtifactDrops']), 2)

    def test_completed_plan_chance(self):
        report, _, _ = store.save_report(self.api, *self.args)
        report['fact'] = report['plan']
        rng = Mock(); rng.random.return_value = .99
        self.assertEqual(store.artifact_drop(self.api, report, rng)['chance'], .2)

    @patch.object(store.requests, 'post')
    def test_vk_message_no_week_and_stable_id(self, post):
        post.return_value.json.return_value = {'response': 123}
        report, _, _ = store.save_report(self.api, *self.args)
        store.send_notification(report); first = post.call_args.kwargs['data'].copy()
        store.send_notification(report); second = post.call_args.kwargs['data']
        self.assertEqual(first['random_id'], second['random_id'])
        self.assertIn('через сайт', first['message'])
        self.assertNotIn('Неделя', first['message'])
        self.assertEqual(first['peer_id'], 2000000002)

    @patch.object(store, '_session')
    @patch.object(store, 'send_notification')
    @patch.object(store, 'artifact_drop', return_value={'success': True, 'dropped': False})
    def test_restart_replays_journal_not_side_effects(self, drop, send, session):
        self.api.get = Mock(return_value={'metadataKey': 'owlet_shared_write_ready', 'metadataValue': 'v1'})
        with patch.object(store, 'Sheets', return_value=self.api):
            first = store.submit_report(report_payload())
            second = store.submit_report(report_payload())
        self.assertEqual(first['notificationStatus'], 'sent')
        self.assertEqual(second['artifactStatus'], 'done')
        drop.assert_called_once(); send.assert_called_once()

    @patch.object(store, '_session')
    @patch.object(store, 'send_notification', side_effect=[requests.Timeout(), None])
    @patch.object(store, 'artifact_drop', return_value={'success': True, 'dropped': False})
    def test_notification_failure_preserves_saved_report(self, drop, send, session):
        self.api.get = Mock(return_value={'metadataKey': 'owlet_shared_write_ready', 'metadataValue': 'v1'})
        with patch.object(store, 'Sheets', return_value=self.api):
            first = store.submit_report(report_payload())
            second = store.submit_report(report_payload())
        self.assertTrue(first['success']); self.assertIn('warning', first)
        self.assertEqual(second['notificationStatus'], 'sent')
        drop.assert_called_once(); self.assertEqual(send.call_count, 2)

    @patch.object(store, '_session')
    def test_incomplete_native_migration_prevents_save(self, session):
        with patch.object(store, 'Sheets', return_value=self.api):
            with patch.dict('os.environ', {'NATIVE_STORAGE_ENABLED': 'false'}):
                with self.assertRaises(store.Busy): store.submit_report(report_payload())
        self.assertFalse(self.api.batches)


class LockTests(unittest.TestCase):
    def test_lock_owner_and_release(self):
        api = store.Sheets(Mock()); api.batch = Mock()
        with api.locked('book'): pass
        create = api.batch.call_args_list[0].args[1][0]['createDeveloperMetadata']['developerMetadata']
        delete = api.batch.call_args_list[1].args[1][0]['deleteDeveloperMetadata']['dataFilter']['developerMetadataLookup']
        self.assertEqual(create['metadataValue'], delete['metadataValue'])
        self.assertEqual(create['visibility'], 'DOCUMENT')

    def test_timeout_retains_lock(self):
        api = store.Sheets(Mock()); api.batch = Mock()
        with api.locked('book'): api.uncertain = True
        self.assertEqual(api.batch.call_count, 1)

    def test_batch_timeout_marks_uncertain(self):
        session = Mock(); session.post.side_effect = requests.Timeout()
        api = store.Sheets(session)
        with self.assertRaises(store.Uncertain): api.batch('book', [])
        self.assertTrue(api.uncertain)


class RecordApiTests(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__); app.register_blueprint(website_api.website_api)
        self.client = app.test_client()
        self.headers = {'X-Website-Password': base64.b64encode('тест'.encode()).decode()}
        self.env = patch.dict('os.environ', {'WEBSITE_SITE_PASSWORD': 'тест', 'WEBSITE_WRITE_ENABLED': 'true'})
        self.env.start(); self.addCleanup(self.env.stop)

    @patch.object(store, 'submit_report')
    def test_auth_precedes_write(self, submit):
        r = self.client.post('/api/website/records', json=report_payload())
        self.assertEqual(r.status_code, 401); submit.assert_not_called()

    @patch.object(store, 'submit_report')
    def test_disabled_gate(self, submit):
        with patch.dict('os.environ', {'WEBSITE_WRITE_ENABLED': 'false'}):
            r = self.client.post('/api/website/records', json=report_payload(), headers=self.headers)
        self.assertEqual(r.status_code, 503); submit.assert_not_called()

    @patch.object(store, 'submit_report', return_value={'success': True})
    def test_save_invalidates_cache_and_cors(self, submit):
        website_api._cache.update(data={'success': True}, expires=999999999, week='old')
        headers = dict(self.headers, Origin='https://anaryann.github.io')
        r = self.client.post('/api/website/records', json=report_payload(), headers=headers)
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(website_api._cache['data'])
        self.assertIn('Content-Type', r.headers['Access-Control-Allow-Headers'])
        self.assertIn('POST', r.headers['Access-Control-Allow-Methods'])

    @patch.object(store, 'submit_report', side_effect=store.Uncertain())
    def test_uncertain_response(self, submit):
        r = self.client.post('/api/website/records', json=report_payload(), headers=self.headers)
        self.assertEqual(r.status_code, 503); self.assertTrue(r.json['uncertain'])


if __name__ == '__main__':
    unittest.main()
