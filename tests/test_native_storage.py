import unittest
from unittest.mock import patch

import native_storage as native
import native_bonus
import website_store as store
from test_website_store import MemorySheets


class NativeAdapterTests(unittest.TestCase):
    def setUp(self):
        self.api = MemorySheets()
        self.env = patch.dict('os.environ', {'NATIVE_STORAGE_ENABLED':'true'})
        self.env.start(); self.addCleanup(self.env.stop)
        for module in (native, native_bonus):
            factory = patch.object(module, '_session'); factory.start(); self.addCleanup(factory.stop)
            sheets = patch.object(module, 'Sheets', return_value=self.api); sheets.start(); self.addCleanup(sheets.stop)

    def test_create_week_once_with_live_participants(self):
        title = native.current_week()['sheetName']
        del self.api.books[store.SHEET_ID][title]
        sid = native.ensure_week(self.api)
        self.assertEqual(self.api.books[store.SHEET_ID][title][7][0],'Тест')
        self.assertEqual(self.api.books[store.SHEET_ID][title][6][0],'Имя')
        self.assertEqual(native.ensure_week(self.api),sid)
        self.assertEqual(len(self.api.books[store.SHEET_ID]['_Служебное']),2)
        batch = self.api.batches[0]
        self.assertIn('addSheet',batch[0])
        self.assertIn('appendCells',batch[-1])

    def test_participants_and_current_read(self):
        self.assertEqual(native.stats_get({'action':'participants'})['people'],['Тест'])
        self.assertEqual(native.stats_get({'action':'current','name':'Тест'})['record']['fact'],10)

    def test_sync_keeps_reported_departed_and_adds_vk_member(self):
        data={'action':'syncParticipants','syncId':'sync-1','members':[{'vkId':'456','name':'Новый'}]}
        result = native.stats_post(data)
        self.assertEqual(result['participantsCount'],1)
        self.assertEqual(result['keptReported'][0]['vkId'],'123')
        rows=self.api.books[store.SHEET_ID][native.current_week()['sheetName']][7:]
        self.assertEqual({r[0] for r in rows if r and r[0]}, {'Тест','Новый'})
        n=len(self.api.batches)
        self.assertEqual(native.stats_post(data),result)
        self.assertEqual(len(self.api.batches),n)

    def test_same_sync_id_different_members_rejected(self):
        data={'action':'syncParticipants','syncId':'s','members':[]}
        native.stats_post(data)
        with self.assertRaises(ValueError):native.stats_post(dict(data,members=[{'name':'A','vkId':'9'}]))

    def test_vk_binding_adds_member_and_week_row(self):
        result=native.stats_post({'action':'vkAutoBind','vkId':'456','firstName':'Анна','lastName':'Тест'})
        self.assertTrue(result['success'])
        self.assertEqual(native.stats_get({'action':'vkUser','vkId':'456'})['name'],'Анна Тест')
        again=native.stats_post({'action':'vkAutoBind','vkId':'456','firstName':'Анна','lastName':'Тест'})
        self.assertTrue(again['alreadyBound'])

    def test_existing_eventlog_survives(self):
        self.api.books[store.SHEET_ID]['EventLog']=[['event_id','timestamp','vk_id','peer_id'],['old','вчера','123','2']]
        self.assertTrue(native.stats_post({'action':'checkEvent','eventId':'old'})['exists'])
        native.stats_post({'action':'markEventProcessed','eventId':'old'})
        self.assertEqual(len(self.api.books[store.SHEET_ID]['EventLog']),2)

    def test_vk_report_uses_persistent_journal(self):
        data={'action':'vkSave','vkId':'123','plan':30,'fact':17,'reportId':'callback-1'}
        first=native.stats_post(data);again=native.stats_post(data)
        self.assertEqual(first,again)
        self.assertEqual(len(self.api.books[store.SHEET_ID]['_WebsiteReports']),2)

    def test_chatlog_deduplicates_and_reads(self):
        import time
        data={'action':'saveMessage','peerId':'2','fromId':'123','timestamp':time.time(),
              'name':'Тест','text':'Привет','eventId':'e1'}
        native.digest_post(data);self.assertTrue(native.digest_post(data)['duplicate'])
        result=native.digest_post({'action':'getMessages','peerId':'2'})
        self.assertEqual(result['count'],1);self.assertEqual(result['messages'][0]['text'],'Привет')

    def test_bonus_legacy_campaign_key_not_awarded_again(self):
        self.api.books[store.ARTIFACTS_ID]['Инвентарь'].append(
            ['123','Тест','1','Перо','обычный',1,'вчера','вчера','bonus::2026-11-10::quest1::123'])
        c={'weekKey':'2026-11-10','eligible':[{'vkId':'123','name':'Тест','fact':17}],'unresolved':[]}
        self.assertEqual(native_bonus.award(c,1,'обычный','quest1',preview=False)['duplicateCount'],1)


if __name__=='__main__': unittest.main()
