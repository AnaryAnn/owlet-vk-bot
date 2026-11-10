import copy
import unittest
from unittest.mock import patch

from native_quests import quest_action, table
from native_treasury import build
from native_storage import people, records, formulas
from native_jobs import run, due_jobs, configured_schedule
from datetime import datetime
from zoneinfo import ZoneInfo


class QuestSheets:
    def __init__(self, status='active', threshold='10', deadline='2099-01-01T00:00:00Z'):
        self.sheets = {
            'Квесты': [['quest_id','название','статус','текущий_этап','peer_id','описание'],
                ['q','Тест',status,'start','2000000002','']],
            'Этапы': [['quest_id','stage_id','тип','текст','порог_мышей','порог_процентов','дедлайн_ISO','успех_этап','провал_этап'],
                ['q','start','start','Начало',threshold,'',deadline,'ok','fail'],
                ['q','ok','finish','Успех','','','','',''],
                ['q','fail','finish','Не успели','','','','','']],
            'Исходящие': [['event_id','quest_id','stage_id','peer_id','текст','статус','создано_ISO','отправлено_ISO']],
            'Журнал': [['event_id','quest_id','stage_id','событие','дата_ISO','детали']],
            'Процентные рубежи': [['event_id','порог_%','текст','peer_id','vk_photo','резерв','статус','дата','неделя_ключ','ошибка'],
                ['m1','25','Четверть','2000000002','','','active','','','']]}
        self.writes = []
    def tabs(self, book):
        return {k: {'sheetId': i} for i,k in enumerate(self.sheets, 1)}
    def values(self, book, ranges, formatted=False):
        return [copy.deepcopy(self.sheets[r.rsplit('!',1)[0].strip("'")]) for r in ranges]
    def batch(self, book, writes):
        self.writes.append(copy.deepcopy(writes))
        by_id = {v['sheetId']: k for k,v in self.tabs(book).items()}
        for w in writes:
            o = w.get('appendCells') or w.get('updateCells')
            sid = o.get('sheetId') or o['start']['sheetId']
            vals = [next(iter(c['userEnteredValue'].values())) for c in o['rows'][0]['values']]
            if 'appendCells' in w:self.sheets[by_id[sid]].append(vals)
            else:
                start=o['start'];r=self.sheets[by_id[sid]][start['rowIndex']]
                col=start['columnIndex'];r.extend(['']*max(0,col+len(vals)-len(r)));r[col:col+len(vals)]=vals


class QuestParityTests(unittest.TestCase):
    def test_start_does_not_finish_same_tick(self):
        api=QuestSheets()
        result=quest_action(api,'book','tick',{'fact':100,'percent':100})
        self.assertEqual(result['transitions'],[{'quest':'q','kind':'start'}])
        self.assertEqual(api.sheets['Квесты'][1][2],'running')
        self.assertEqual(result['outbox'][0]['eventId'],'q:start:start')
        self.assertEqual(len(api.writes),1)
        self.assertEqual(len(api.writes[0]),3)
    def test_success_and_retry_do_not_duplicate_outbox(self):
        api=QuestSheets(status='running')
        result=quest_action(api,'book','tick',{'fact':10,'percent':None})
        self.assertTrue(result['transitions'][0]['success'])
        self.assertEqual(api.sheets['Квесты'][1][2:4],['finished','ok'])
        again=quest_action(api,'book','tick',{'fact':10})
        self.assertEqual(again['transitions'],[])
        self.assertEqual(len(api.sheets['Исходящие']),2)
    def test_failure_only_after_deadline(self):
        api=QuestSheets(status='running')
        self.assertEqual(quest_action(api,'b','tick',{'fact':0})['transitions'],[])
        api.sheets['Этапы'][1][6]='2000-01-01T00:00:00Z'
        result=quest_action(api,'b','tick',{'fact':0})
        self.assertFalse(result['transitions'][0]['success'])
        self.assertEqual(api.sheets['Квесты'][1][3],'fail')
    def test_dry_run_does_not_mutate(self):
        api=QuestSheets();before=copy.deepcopy(api.sheets)
        quest_action(api,'b','tick',{'fact':100,'dryRun':True})
        self.assertEqual(api.sheets,before);self.assertFalse(api.writes)
    def test_ack_is_idempotent(self):
        api=QuestSheets();result=quest_action(api,'b','tick',{'fact':0})
        event=result['outbox'][0]['eventId']
        quest_action(api,'b','ack',{'eventId':event});n=len(api.writes)
        quest_action(api,'b','ack',{'eventId':event})
        self.assertEqual(len(api.writes),n)
        self.assertEqual(api.sheets['Исходящие'][1][5],'sent')
    def test_milestone_claim_retry_and_ack(self):
        api=QuestSheets();data={'percent':25,'weekKey':'2026-11-10'}
        first=quest_action(api,'b','milestoneClaim',data)
        self.assertEqual(first['event']['eventId'],'m1')
        self.assertEqual(quest_action(api,'b','milestoneClaim',data),first)
        quest_action(api,'b','milestoneAck',{'eventId':'m1','weekKey':'2026-11-10'})
        self.assertIsNone(quest_action(api,'b','milestoneClaim',data)['event'])
    def test_actual_sheet_row_after_empty_row(self):
        api=QuestSheets();api.sheets['Квесты'].insert(1,[])
        self.assertEqual(table(api,'b','Квесты')[1][0]['_row'],2)
    def test_invalid_fact(self):
        with self.assertRaises(ValueError):quest_action(QuestSheets(),'b','tick',{'fact':float('nan')})


class TreasuryParityTests(unittest.TestCase):
    def setUp(self):
        self.catalog=[[1,'обычный','Перо','Текст','photo-1_1','https://res.cloudinary.com/jlcfrihy/image/upload/one'],
                      [2,'редкий','Ненайденный','','','https://res.cloudinary.com/jlcfrihy/image/upload/two'],
                      [3,'редкий','Ключ','','','https://untrusted.example/key']]
        self.inventory=[['123','Анна',1,'Перо','обычный',2],['123','Анна',1,'Перо','обычный',1],
                        ['456','Дима',3,'Ключ','редкий',1]]
    def test_undiscovered_cards_never_exposed(self):
        result=build('collection','123',self.inventory,self.catalog)
        self.assertEqual([r['id'] for r in result['common']['cards']],[1,3])
        self.assertEqual(result['personal']['cards'][0]['count'],3)
    def test_only_approved_image_origin(self):
        result=build('collection','0',self.inventory,self.catalog)
        self.assertTrue(result['common']['cards'][0]['hasImage'])
        self.assertFalse(result['common']['cards'][1]['hasImage'])
    def test_participants_are_inventory_owners(self):
        result=build('participants','0',self.inventory,self.catalog)
        self.assertEqual(len(result['participants']),2)
    def test_invalid_owner(self):
        with self.assertRaises(ValueError):build('collection','abc',self.inventory,self.catalog)


class NativeUtilityTests(unittest.TestCase):
    def test_status_and_record_zero(self):
        self.assertEqual(people([['Анна','123','выбыл']])[0]['status'],'выбыл')
        week={'start':'2026-11-10','end':'2026-11-16'}
        self.assertEqual(len(records([['Анна',0,0,'сегодня']],week)),1)
    def test_formulas_do_not_include_summary_rows(self):
        self.assertEqual(formulas(1,192)['updateCells']['rows'][0]['values'][0]['userEnteredValue']['formulaValue'],'=SUM(B8:B)')
    def test_cron_dry_run_never_imports_app_or_writes(self):
        result=run('statistics',dry_run=True)
        self.assertTrue(result['dryRun']);self.assertIn('statistics:',result['key'])
    def test_schedule_uses_confirmed_timezone_and_time(self):
        with patch.dict('os.environ',{},clear=True): schedule=configured_schedule()
        tz=ZoneInfo('Europe/Moscow')
        for day in (6,7,8,9):
            self.assertIn('statistics',due_jobs(schedule,datetime(2026,11,day,18,0,tzinfo=tz)))
        for day in (10,11,12):
            self.assertNotIn('statistics',due_jobs(schedule,datetime(2026,11,day,18,0,tzinfo=tz)))
        self.assertNotIn('statistics',due_jobs(schedule,datetime(2026,11,9,17,59,tzinfo=tz)))
        for hour in (0,4,8,12,16,20):
            self.assertIn('sync',due_jobs(schedule,datetime(2026,11,9,hour,0,tzinfo=tz)))
        self.assertNotIn('sync',due_jobs(schedule,datetime(2026,11,9,2,0,tzinfo=tz)))
        self.assertEqual(due_jobs([],datetime.now()),[])
