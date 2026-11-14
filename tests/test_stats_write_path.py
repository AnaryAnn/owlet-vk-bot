import unittest
from unittest.mock import Mock, patch
import requests
import website_store as store


class StatsWritePathTests(unittest.TestCase):
    def api(self):
        api = store.Sheets(Mock())
        api.tabs = Mock(return_value={'week': {'sheetId': 1}, 'journal': {'sheetId': 2}})
        api.values = Mock(return_value=[[['header'], ['old'], ['old2']]])
        api.get = Mock(return_value={'sheets': [
            {'properties': {'sheetId': sid, 'gridProperties': {'rowCount': 1000, 'columnCount': 8}}}
            for sid in (1, 2)]})
        committed = Mock(status_code=200)
        committed.json.return_value = {'totalUpdatedCells': 6}
        api.session.post.return_value = committed
        return api

    def test_data_and_dedup_result_commit_in_one_raw_batch(self):
        api = self.api()
        api.batch(store.SHEET_ID, [store.update(1, 7, 1, [30, 17, 'stamp']),
            store.append(2, ['report-123', 'fingerprint', '{"success":true}'])])
        calls = api.session.post.call_args_list
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0].args[0].endswith('/values:batchUpdate'))
        payload = calls[0].kwargs['json']
        self.assertEqual(payload['valueInputOption'], 'RAW')
        self.assertEqual(payload['data'], [
            {'range': "'week'!B8:D8", 'values': [[30, 17, 'stamp']]},
            {'range': "'journal'!A4:C4", 'values': [['report-123', 'fingerprint', '{"success":true}']]}])

    def test_uncertain_commit_is_not_retried(self):
        api = self.api()
        api.session.post.side_effect = requests.ReadTimeout()
        with self.assertRaises(store.Uncertain):
            api.batch(store.SHEET_ID, [store.append(2, ['id', 'fingerprint', 'result'])])
        self.assertTrue(api.uncertain)
        self.assertEqual(api.session.post.call_count, 1)

    def test_sparse_rows_and_two_appends_use_explicit_distinct_rows(self):
        api = self.api()
        api.values.return_value = [[['header'], [], ['', '', '', '', 'occupied']]]
        api.batch(store.SHEET_ID, [store.append(2, ['one']), store.append(2, ['two'])])
        data = api.session.post.call_args.kwargs['json']['data']
        self.assertEqual([d['range'] for d in data], ["'journal'!A4:A4", "'journal'!A5:A5"])
        self.assertEqual(api.values.call_args.args[1], ["'journal'!A:H"])

    def test_capacity_growth_contains_no_data_then_single_atomic_commit(self):
        api = self.api()
        api.get.return_value['sheets'][1]['properties']['gridProperties']['rowCount'] = 3
        api.batch(store.SHEET_ID, [store.append(2, ['id', 'digest', 'result'])])
        calls = api.session.post.call_args_list
        self.assertEqual(calls[0].kwargs['json'], {'requests': [
            {'appendDimension': {'sheetId': 2, 'dimension': 'ROWS', 'length': 1}}]})
        self.assertTrue(calls[1].args[0].endswith('/values:batchUpdate'))

    def test_stats_lock_is_separate_from_quest_lock_and_owner_checked_on_release(self):
        api = store.Sheets(Mock())
        api.batch = Mock(return_value={})
        with patch.object(store, 'STATS_LOCK_BOOK', 'healthy-book'):
            with api.locked(store.SHEET_ID):
                pass
        create, delete = api.batch.call_args_list
        self.assertEqual(create.args[0], 'healthy-book')
        metadata = create.args[1][0]['createDeveloperMetadata']['developerMetadata']
        lookup = delete.args[1][0]['deleteDeveloperMetadata']['dataFilter']['developerMetadataLookup']
        self.assertNotEqual(metadata['metadataId'], store.LOCK_ID)
        self.assertEqual(lookup['metadataValue'], metadata['metadataValue'])
