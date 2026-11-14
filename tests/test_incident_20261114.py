import unittest
from contextlib import nullcontext
from unittest.mock import Mock, patch

import incident_20261114 as incident


class IncidentTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(incident, 'STATS_LOCK_BOOK', '1k2cIfXXyCGIrTzUnodYR9STXNkR00LhDzI60mpCVaXw')
        patcher.start()
        self.addCleanup(patcher.stop)
    def api(self):
        api = Mock()
        api.tabs.return_value = {'Участники': {}, '_WebsiteReports': {}}
        api.values.side_effect = [[[['header'], ['reserved-placeholder']][i] for i in range(2)]], [[]]
        api.locked.return_value = nullcontext()
        return api

    def test_removes_only_observed_stats_owner_and_marks_same_batch(self):
        api = self.api()
        lock = {'metadataId': incident.STATS_LOCK_ID, 'metadataKey': incident.STATS_LOCK_KEY,
                'metadataValue': 'incident-owner'}
        api.get.side_effect = [{'developerMetadata': [lock]}, {'developerMetadata': [lock]}, {}]
        result = incident.repair(api)
        self.assertTrue(result['success'])
        writes = api.batch.call_args.args[1]
        lookup = writes[0]['deleteDeveloperMetadata']['dataFilter']['developerMetadataLookup']
        self.assertEqual(lookup['metadataValue'], 'incident-owner')
        self.assertEqual(lookup['metadataId'], incident.STATS_LOCK_ID)
        self.assertEqual(writes[1]['createDeveloperMetadata']['developerMetadata']['metadataId'], incident.MARKER_ID)
        self.assertEqual(len(writes), 2)

    def test_persistent_marker_prevents_future_unlocks(self):
        api = self.api()
        api.get.return_value = {'developerMetadata': [{'metadataId': incident.MARKER_ID}]}
        self.assertTrue(incident.repair(api)['alreadyRepaired'])
        api.batch.assert_not_called()

    def test_changed_owner_is_never_removed(self):
        api = self.api()
        lock = {'metadataId': incident.STATS_LOCK_ID, 'metadataKey': incident.STATS_LOCK_KEY,
                'metadataValue': 'old'}
        api.get.side_effect = [{'developerMetadata': [lock]},
                              {'developerMetadata': [dict(lock, metadataValue='new')]}]
        with self.assertRaises(RuntimeError):
            incident.repair(api)
        api.batch.assert_not_called()
