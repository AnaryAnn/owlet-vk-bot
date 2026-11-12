import unittest
from contextlib import contextmanager
from unittest.mock import Mock, patch
import requests
import native_quests as quests
import website_store as store


class GoogleLockErrors(unittest.TestCase):
    def error(self, text, status=400):
        response = Mock(status_code=status)
        response.json.return_value = {'error': {'message': text}}
        return requests.HTTPError(response=response)
    def test_duplicate_error_is_busy_even_if_lock_was_just_released(self):
        api = store.Sheets(Mock())
        api.batch = Mock(side_effect=self.error(f'A developer metadata with ID {store.LOCK_ID} already exists.'))
        api.get = Mock()
        with self.assertRaises(store.Busy):
            with api.locked('book'): self.fail('entered lock')
        api.get.assert_not_called()
    def test_generic_400_with_existing_own_lock_is_busy(self):
        api = store.Sheets(Mock())
        api.batch = Mock(side_effect=self.error('Invalid request'))
        api.get = Mock(return_value={'developerMetadata': [dict(metadataId=store.LOCK_ID, metadataKey=store.LOCK_KEY)]})
        with self.assertRaises(store.Busy):
            with api.locked('book'): self.fail('entered lock')
    def test_other_400_is_not_disguised_as_contention(self):
        api = store.Sheets(Mock())
        failure = self.error('Invalid metadata location')
        api.batch = Mock(side_effect=failure)
        api.get = Mock(return_value={'developerMetadata': []})
        with self.assertRaises(requests.HTTPError) as caught:
            with api.locked('book'): self.fail('entered lock')
        self.assertIs(caught.exception, failure)
    def test_lock_id_owned_by_another_system_is_not_busy(self):
        api = store.Sheets(Mock())
        api.batch = Mock(side_effect=self.error('Invalid request'))
        api.get = Mock(return_value={'developerMetadata': [dict(metadataId=store.LOCK_ID, metadataKey='different-app')]})
        with self.assertRaises(requests.HTTPError):
            with api.locked('book'): self.fail('entered lock')
    def test_permission_failure_not_retried(self):
        api = store.Sheets(Mock())
        api.batch = Mock(side_effect=self.error('Forbidden', 403))
        api.get = Mock()
        with self.assertRaises(requests.HTTPError):
            with api.locked('book'): self.fail('entered lock')
        api.get.assert_not_called()


class QuestRetryTests(unittest.TestCase):
    def run_case(self, failures):
        attempts = []
        @contextmanager
        def locked(book):
            attempts.append(book)
            if failures:
                failure = failures.pop(0)
                if failure: raise failure
            yield
        api = Mock(); api.locked.side_effect = locked
        with patch.dict('os.environ', {'NATIVE_STORAGE_ENABLED': 'true', 'QUEST_SPREADSHEET_ID': 'book'}), \
             patch.object(quests, '_session'), patch.object(quests, 'Sheets', return_value=api), \
             patch.object(quests, 'quest_action', return_value={'success': True}) as operation, \
             patch.object(quests.time, 'sleep') as sleep:
            try:
                result = quests.dispatch('vote', questId='q')
                return result, attempts, operation.call_count, sleep.call_count
            except Exception:
                self.last = (attempts, operation.call_count, sleep.call_count)
                raise
    def test_busy_then_success_executes_vote_once(self):
        result, attempts, calls, waits = self.run_case([store.Busy('busy'), None])
        self.assertTrue(result['success']); self.assertEqual(len(attempts), 2)
        self.assertEqual(calls, 1); self.assertEqual(waits, 1)
    def test_persistent_lock_is_not_deleted_or_bypassed(self):
        with self.assertRaises(store.Busy): self.run_case([store.Busy('busy')] * 5)
        self.assertEqual(self.last, (['book'] * 5, 0, 4))
    def test_uncertain_result_is_not_retried(self):
        with self.assertRaises(store.Uncertain): self.run_case([store.Uncertain('unknown')])
        self.assertEqual(self.last, (['book'], 0, 0))
