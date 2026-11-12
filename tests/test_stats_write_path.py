import unittest
from unittest.mock import Mock, patch
import requests
import website_store as store


class StatsWritePathTests(unittest.TestCase):
    def api(self):
        api = store.Sheets(Mock())
        api.tabs = Mock(return_value={'week': {'sheetId': 1}, 'journal': {'sheetId': 2}})
        api.values = Mock(return_value=[[['header'], ['old'], ['old2']]])
        reserved = Mock(status_code=200)
        reserved.json.return_value = {'updates': {'updatedRange': "'journal'!A4"}}
        committed = Mock(status_code=200)
        committed.json.return_value = {'totalUpdatedCells': 6}
        api.session.post.side_effect = [reserved, committed]
        return api

    def test_data_and_dedup_result_commit_in_one_raw_batch(self):
        api = self.api()
        api.batch(store.SHEET_ID, [store.update(1, 7, 1, [30, 17, 'stamp']),
            store.append(2, ['report-123', 'fingerprint', '{"success":true}'])])
        calls = api.session.post.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0].kwargs['json']['values'][0][0].startswith('reserved-'))
        self.assertNotIn('report-123', str(calls[0]))
        self.assertTrue(calls[1].args[0].endswith('/values:batchUpdate'))
        payload = calls[1].kwargs['json']
        self.assertEqual(payload['valueInputOption'], 'RAW')
        self.assertEqual(payload['data'], [
            {'range': "'week'!B8:D8", 'values': [[30, 17, 'stamp']]},
            {'range': "'journal'!A4:C4", 'values': [['report-123', 'fingerprint', '{"success":true}']]}])

    def test_uncertain_commit_is_not_retried(self):
        api = self.api()
        reservation = api.session.post.side_effect
        # Keep the successful capacity reservation, then lose the commit response.
        api.session.post.side_effect = [next(reservation), requests.ReadTimeout()]
        with self.assertRaises(store.Uncertain):
            api.batch(store.SHEET_ID, [store.append(2, ['id', 'fingerprint', 'result'])])
        self.assertTrue(api.uncertain)
        self.assertEqual(api.session.post.call_count, 2)

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
