"""Run separately against the local server: python3 tests/http_smoke.py."""
import http.cookiejar
import json
import os
import time
import unittest
import urllib.error
import urllib.request
import uuid

BASE = os.environ.get('PITWALL_URL', 'http://127.0.0.1:8001')


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.browser = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.request('/api/bootstrap')

    def request(self, path, data=None, headers=None, browser=None):
        hdr = {'Content-Type': 'application/json'} if data is not None else {}
        hdr.update(headers or {})
        body = json.dumps(data).encode() if data is not None else None
        request = urllib.request.Request(BASE + path, data=body, headers=hdr)
        try:
            with (browser or self.browser).open(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def create(self):
        code, run = self.request('/api/runs', {'lap':20,'request_id':uuid.uuid4().hex})
        self.assertEqual(code, 201)
        return run

    def test_health_and_bootstrap(self):
        self.assertEqual(self.request('/health'), (200, {'status':'ok'}))
        code, data = self.request('/api/bootstrap')
        self.assertEqual(code, 200);self.assertEqual(data['meta']['laps'], 56)
        self.assertNotIn('api_key', json.dumps(data).lower())

    def test_agent_test_is_read_only_exportable_and_owner_scoped(self):
        run=self.create();prefix='/api/runs/'+run['run_id']
        code,test=self.request(prefix+'/test',{'request_id':uuid.uuid4().hex,'expected_version':run['version'],'case':'puncture','target':'all','mode':'local'})
        self.assertEqual(code,202)
        path='/api/agent-tests/'+test['test_id'];deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            test=self.request(path)[1]
            if test['status'] not in ('queued','running'):break
            time.sleep(.02)
        self.assertEqual(test['status'],'completed');self.assertEqual(test['result']['verdict'],'pass')
        self.assertEqual(test['result']['decision']['recommended'],'pit_now')
        self.assertEqual(self.request(prefix)[1],run)
        self.assertEqual(self.request(path+'/export')[1],test)
        other=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.assertEqual(self.request(path,browser=other)[0],404)
        self.assertEqual(self.request(path+'/export',browser=other)[0],404)

    def test_agent_test_rejects_invalid_case_and_stale_version(self):
        run=self.create();path='/api/runs/'+run['run_id']+'/test'
        self.assertEqual(self.request(path,{'request_id':uuid.uuid4().hex,'expected_version':0,'case':'unknown'})[0],400)
        self.assertEqual(self.request(path,{'request_id':uuid.uuid4().hex,'expected_version':9,'case':'current'})[0],409)

    def test_stop_is_terminal_and_reset_creates_a_fresh_saved_run(self):
        run=self.create();path='/api/runs/'+run['run_id']
        code,stopped=self.request(path+'/playback',{'request_id':uuid.uuid4().hex,'expected_version':0,'action':'stop','speed':1})
        self.assertEqual(code,200);self.assertEqual(stopped['branch']['playback']['status'],'stopped')
        self.assertEqual(self.request(path+'/playback',{'request_id':uuid.uuid4().hex,'expected_version':stopped['version'],'action':'start','speed':1})[0],409)
        code,fresh=self.request('/api/runs',{'lap':1,'request_id':uuid.uuid4().hex})
        self.assertEqual(code,201);self.assertEqual(fresh['branch']['lap'],1);self.assertEqual(fresh['memory'],[])
        self.assertEqual(self.request(path)[1],stopped)

    def test_session_isolation_on_read_and_export(self):
        run = self.create()
        other = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        for suffix in ('', '/export'):
            self.assertEqual(self.request('/api/runs/'+run['run_id']+suffix, browser=other)[0], 404)

    def test_invalid_lap_does_not_create_a_run(self):
        self.assertEqual(self.request('/api/runs', {'lap':57,'request_id':uuid.uuid4().hex})[0], 400)
        self.assertEqual(self.request('/api/bootstrap')[1]['runs'], [])

    def test_stale_version_and_retry(self):
        run=self.create();path='/api/runs/'+run['run_id']+'/remember'
        data={'text':'Keep this instruction','request_id':'note-once','expected_version':0}
        code, result=self.request(path,data);self.assertEqual(code, 200)
        self.assertEqual(self.request(path,data)[1],result)
        data['request_id']='stale';self.assertEqual(self.request(path,data)[0],409)
        self.assertEqual(len(self.request('/api/runs/'+run['run_id'])[1]['memory']),1)

    def test_start_pause_controls_are_persisted(self):
        run=self.create();path='/api/runs/'+run['run_id']+'/playback'
        code,started=self.request(path,{'action':'start','speed':80,'request_id':'start','expected_version':0})
        self.assertEqual(code,200);self.assertEqual(started['branch']['playback']['status'],'running')
        code,paused=self.request(path,{'action':'pause','speed':80,'request_id':'pause','expected_version':started['version']})
        self.assertEqual(code,200);self.assertEqual(paused['branch']['playback']['status'],'paused')

    def test_direct_jump_returns_current_three_agent_evidence(self):
        run=self.create();prefix='/api/runs/'+run['run_id']
        code,local=self.request(prefix+'/mode',{'mode':'local','request_id':'mode','expected_version':0})
        self.assertEqual(code,200)
        code,result=self.request(prefix+'/jump',{'lap':30,'monitor':False,'refresh':True,'request_id':'jump','expected_version':local['version']})
        self.assertEqual(code,200);self.assertEqual(result['branch']['lap'],30)
        self.assertEqual(result['branch']['pit_count'],0)
        reports=result['branch']['telemetry']['reports']
        self.assertEqual(len(reports),3);self.assertTrue(all(r['as_of']==result['as_of'] for r in reports))

    def test_read_only_pit_preview_returns_position_and_costs(self):
        run=self.create();prefix='/api/runs/'+run['run_id']
        code,result=self.request(prefix+'/preview?compound=HARD&repair_wing=false')
        self.assertEqual(code,200);self.assertEqual(result['forecast']['compound'],'HARD')
        self.assertIn('pit_exit_position',result['forecast'])
        flags=result['forecast']['cost_by_flag']
        self.assertLess(flags['SC']['total_loss_seconds'],flags['VSC']['total_loss_seconds'])
        self.assertEqual(self.request(prefix)[1]['version'],run['version'])
        self.assertEqual(self.request(prefix+'/preview?compound=MAGIC')[0],400)
        self.assertEqual(self.request(prefix+'/preview?repair_wing=invalid')[0],400)

    def test_other_browser_cannot_read_pit_preview(self):
        run=self.create()
        other=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.assertEqual(self.request('/api/runs/'+run['run_id']+'/preview',browser=other)[0],404)

    def test_cross_origin_and_rebinding_are_rejected(self):
        payload={'lap':20,'request_id':uuid.uuid4().hex}
        self.assertEqual(self.request('/api/runs',payload,{'Origin':'https://other.example'})[0],403)
        self.assertEqual(self.request('/api/bootstrap',headers={'Host':'other.example'})[0],421)

    def test_archive_database_and_credentials_are_not_served(self):
        for path in ('/data/spa-2025-demo.json','/.local/web.sqlite3','/.env','/../pitwall/service.py'):
            with self.subTest(path=path):
                self.assertEqual(self.request(path)[0],404)

    def test_non_json_post_is_rejected(self):
        self.assertEqual(self.request('/api/runs',{'lap':20}, {'Content-Type':'text/plain'})[0],415)

    def test_missing_version_and_invalid_action_return_json_errors(self):
        run=self.create();prefix='/api/runs/'+run['run_id']
        self.assertEqual(self.request(prefix+'/jump',{'lap':21,'request_id':'missing-version'})[0],400)
        self.assertEqual(self.request(prefix+'/unknown',{'request_id':'x'})[0],404)
        self.assertEqual(self.request(prefix+'/assess',{'mode':'invented','request_id':'bad','expected_version':0})[0],400)


if __name__ == '__main__':
    unittest.main(verbosity=2)
