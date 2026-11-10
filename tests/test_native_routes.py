import base64
import unittest
from unittest.mock import patch
from pathlib import Path

import app
import website_api


class NativeRouteTests(unittest.TestCase):
    def setUp(self):
        self.client=app.app.test_client()
        env=patch.dict('os.environ',{'WEBSITE_SITE_PASSWORD':'обычный','WEBSITE_ADMIN_PASSWORD':'админ'})
        env.start();self.addCleanup(env.stop)
    def headers(self,p):return {'X-Website-Password':base64.b64encode(p.encode()).decode()}
    def test_login_admin_role_checked_on_server(self):
        self.assertFalse(self.client.get('/api/website/login',headers=self.headers('обычный')).json['isAdmin'])
        self.assertTrue(self.client.get('/api/website/login',headers=self.headers('админ')).json['isAdmin'])
        self.assertEqual(self.client.get('/api/website/login',headers=self.headers('wrong')).status_code,401)
    @patch('native_preflight.check',return_value={'success':True,'books':[]})
    def test_preflight_read_only_requires_admin(self,check):
        self.assertEqual(self.client.get('/api/website/preflight',headers=self.headers('обычный')).status_code,403)
        check.assert_not_called()
        result=self.client.get('/api/website/preflight',headers=self.headers('админ'))
        self.assertEqual(result.status_code,200);self.assertTrue(result.json['success'])
        check.assert_called_once()
    def test_guest_cannot_modify_goal_or_award_bonus(self):
        for route in ('settings','bonus'):
            r=self.client.post('/api/website/'+route,json={'goal':100},headers=self.headers('обычный'))
            self.assertEqual(r.status_code,403)
    @patch('native_storage.stats_post',return_value={'success':True,'teamGoal':100})
    def test_admin_goal_calls_native_storage(self,post):
        r=self.client.post('/api/website/settings',json={'goal':100},headers=self.headers('админ'))
        self.assertEqual(r.status_code,200);post.assert_called_once_with({'action':'saveGoal','goal':100})
    @patch('native_treasury.read',return_value={'ok':True,'total':200})
    def test_public_read_only_treasury_contract(self,read):
        r=self.client.get('/api/treasury?action=collection&vkId=123',headers={'Origin':'https://anaryann.github.io'})
        self.assertEqual(r.status_code,200);self.assertTrue(r.json['ok'])
        self.assertEqual(r.headers['Access-Control-Allow-Origin'],'https://anaryann.github.io')
        self.assertEqual(self.client.post('/api/treasury').status_code,405)
    @patch('native_storage.stats_get',return_value={'success':True})
    @patch('native_storage.stats_post',return_value={'success':True})
    @patch('native_storage.digest_post',return_value={'success':True})
    def test_bot_adapters_do_not_use_http_scripts(self,digest,post,get):
        self.assertTrue(app.google_get({'action':'stats'})['success'])
        self.assertTrue(app.google_save_report('123',30,17,'report1')['success'])
        self.assertTrue(app.google_sync_participants([],'sync1')['success'])
        self.assertTrue(app.digest_post({'action':'getMessages'})['success'])
        self.assertEqual(post.call_count,2);get.assert_called_once();digest.assert_called_once()
    def test_empty_schedule_sends_nothing_and_requires_secret(self):
        with patch.object(app,'SCHEDULE_SECRET','test'),patch.dict('os.environ',{'NATIVE_SCHEDULES_JSON':'[]'}):
            self.assertEqual(self.client.post('/native-schedule',json={}).status_code,403)
            result=self.client.post('/native-schedule',json={},headers={'X-Schedule-Secret':'test'})
            self.assertEqual(result.json,{'success':True,'jobs':[]})
    def test_runtime_has_no_apps_script_addresses(self):
        root=Path(__file__).resolve().parents[1]
        for name in ('app.py','quest_engine.py','native_storage.py','native_quests.py','native_treasury.py'):
            source=(root/name).read_text()
            self.assertNotIn('script.google.com',source)
            self.assertNotIn('SCRIPT_URL',source)


if __name__=='__main__':unittest.main()
