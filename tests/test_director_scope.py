"""Behavioral regressions for the human-controlled, three-specialist simulator."""
import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from pitwall.agents import StrategyWorkflow, ResponsesClient
from pitwall.data import ROOT, load_timeline, parse_time
from pitwall.service import PitwallService, Conflict
from pitwall.simulation import advance_lap, active_damage, decision_trigger, apply_conditions, MODEL
from pitwall.specialists import Specialist, ROLE_TOOLS
from pitwall.rivals import RivalFeed


class DirectorScopeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.timeline=load_timeline(ROOT/'data/spa-2025-demo.json')
        cls.rivals=json.loads((ROOT/'data/spa-2025-rivals.json').read_text())

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'test.sqlite'
        self.service=PitwallService(self.path,self.timeline,client=ResponsesClient(api_key=''),rivals=self.rivals)
        self.owner='test-browser';self.run=self.service.create(self.owner,20,'new')
        self.rid=self.run['run_id'];self.serial=0
        self.addCleanup(self.temp.cleanup);self.addCleanup(self.service.close)

    def key(self):
        self.serial+=1;return 'action-'+str(self.serial)

    def get(self):return self.service.get(self.owner,self.rid)

    def wait(self):
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            r=self.get()
            if r['job'] and r['job']['status'] not in ('queued','running'):
                self.assertEqual(r['job']['status'],'completed');return r
            time.sleep(.01)
        self.fail('Job did not complete')

    def incident(self,kind):
        r=self.get();self.service.incident(self.owner,self.rid,kind,self.key(),r['version']);return self.wait()

    def decide(self,choice,repair=True):
        r=self.get()
        if r['branch']['assessment']['status']=='automatic':
            r=self.service.request_review(self.owner,self.rid,self.key(),r['version'])
        return self.service.decide(self.owner,self.rid,choice,'MEDIUM','Explicit test call',self.key(),r['version'],repair)

    def test_three_specialists_use_only_their_own_tools(self):
        result=StrategyWorkflow(self.run['observed'],self.run['branch'],[]).run()
        self.assertEqual({r['role'] for r in result['specialists']},set(ROLE_TOOLS))
        for role,names in ROLE_TOOLS.items():
            tools={e['tool'] for e in result['trace'] if e['role']==role}
            self.assertTrue(set(names).issubset(tools))
            worker=Specialist(role,self.run['observed'],self.run['branch'],lambda *args:None)
            foreign=next(t for other,ts in ROLE_TOOLS.items() if other!=role for t in ts)
            with self.assertRaises(ValueError):worker.tool(foreign)

    def test_director_cannot_skip_any_specialist(self):
        w=StrategyWorkflow(self.run['observed'],self.run['branch'],[])
        for tool,args in [('recall_memory',{}),('delegate_rival_analyst',{}),('delegate_conditions_agent',{}),('compare_strategies',{'stress':'baseline'})]:w.call(tool,args)
        with self.assertRaises(ValueError):w.call('propose_decision',{'choice':'hold','rationale':'Skip telemetry','needs_human':False})

    def test_imported_leclerc_stop_never_changes_simulated_car(self):
        observed=self.service.observed(10);branch=copy.deepcopy(self.run['branch'])
        branch.update(lap=10,entry_lap=10,compound='HARD',tyre_age=2,history=[])
        for lap in range(10,15):advance_lap(branch,self.service.observed(lap))
        self.assertEqual(branch['pit_count'],0);self.assertEqual(branch['compound'],'HARD');self.assertEqual(branch['tyre_age'],7)
        self.assertEqual(self.service.observed(13)['observed_tyre_age'],0)

    def test_unauthorized_queue_cannot_execute(self):
        b=copy.deepcopy(self.run['branch']);b['queued_pit']={'lap':20,'compound':'HARD','authorized_by':'agent'}
        with self.assertRaisesRegex(ValueError,'human authorization'):advance_lap(b,self.run['observed'])
        self.assertEqual(b['pit_count'],0)

    def test_puncture_recommends_box_but_does_not_box(self):
        r=self.incident('puncture')
        self.assertEqual(r['branch']['assessment']['recommended'],'pit_now')
        self.assertEqual(r['branch']['assessment']['status'],'awaiting_human')
        self.assertEqual(r['branch']['pit_count'],0);self.assertIsNone(r['branch']['queued_pit'])
        self.assertEqual(len(active_damage(r['branch'])),1)
        self.assertTrue(any('puncture' in s['summary'].lower() for s in r['branch']['assessment']['specialists']))

    def test_human_can_stay_out_and_damage_costs_time(self):
        self.incident('front_wing');r=self.decide('hold')
        r=self.service.jump(self.owner,self.rid,21,self.key(),r['version'])
        self.assertEqual(r['branch']['pit_count'],0);self.assertEqual(len(active_damage(r['branch'])),1)
        self.assertEqual(r['branch']['history'][0]['damage_seconds'],MODEL['damage_seconds_per_lap']['front_wing'])
        healthy=copy.deepcopy(self.run['branch']);advance_lap(healthy,self.run['observed'])
        self.assertGreater(r['branch']['delta_seconds'],healthy['delta_seconds'])

    def test_only_approved_repair_clears_wing_and_puncture(self):
        self.incident('front_wing');self.incident('puncture');r=self.decide('pit_now')
        r=self.service.jump(self.owner,self.rid,21,self.key(),r['version'])
        self.assertEqual(r['branch']['pit_count'],1);self.assertEqual(active_damage(r['branch']),[])
        self.assertEqual(set(r['branch']['history'][0]['repaired']),{'front_wing','puncture'})
        self.assertTrue(any(m['kind']=='pit_executed' for m in r['memory']))

    def test_tyre_only_stop_keeps_wing_damage(self):
        self.incident('front_wing');self.incident('puncture');r=self.decide('pit_now',repair=False)
        r=self.service.jump(self.owner,self.rid,21,self.key(),r['version'])
        self.assertEqual([i['type'] for i in active_damage(r['branch'])],['front_wing'])
        self.assertEqual(r['branch']['history'][0]['repaired'],['puncture'])

    def test_jump_pauses_at_first_decision_before_target_and_retains_memory(self):
        r=self.service.create(self.owner,1,'from-start');self.rid=r['run_id']
        r=self.service.remember(self.owner,self.rid,'Protect track position',self.key(),r['version'])
        expected=None;b=copy.deepcopy(r['branch'])
        for lap in range(1,21):
            if decision_trigger(b,self.service.observed(lap)):
                expected=lap;break
            advance_lap(b,self.service.observed(lap))
        self.assertIsNotNone(expected);self.assertLess(expected,20)
        r=self.service.jump(self.owner,self.rid,20,self.key(),r['version'])
        self.assertEqual(r['branch']['lap'],expected);self.assertEqual(r['branch']['target_lap'],20)
        r=self.wait();self.assertEqual(r['branch']['pit_count'],0)
        self.assertEqual(r['branch']['assessment']['status'],'awaiting_human')
        self.assertTrue(any('Protect track position' in m['content'] for m in r['memory']))
        with self.assertRaises(Conflict):self.service.jump(self.owner,self.rid,20,self.key(),r['version'])

    def test_incident_retry_does_not_duplicate_memory_or_job(self):
        first=self.service.incident(self.owner,self.rid,'puncture','once',0)
        self.wait();retry=self.service.incident(self.owner,self.rid,'puncture','once',0)
        self.assertEqual(first,retry)
        r=self.get();self.assertEqual(len(r['branch']['incidents']),1);self.assertEqual(len(r['memory']),1)
        with self.service.replay._connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM web_jobs').fetchone()[0],1)

    def test_invalid_incident_rolls_back_without_erasing_advice(self):
        before=self.get()
        with self.assertRaises(ValueError):self.service.incident(self.owner,self.rid,'teleport','bad',0)
        self.assertEqual(self.get(),before)

    def test_restart_preserves_incident_pause_target_and_decision(self):
        r=self.incident('puncture')
        other=PitwallService(self.path,self.timeline,client=ResponsesClient(api_key=''),rivals=self.rivals)
        try:self.assertEqual(other.get(self.owner,self.rid),r)
        finally:other.close()

    def test_manual_weather_and_safety_car_feed_specialists_and_model(self):
        r=self.incident('rain');self.assertEqual(r['observed']['condition'],'rain')
        self.assertEqual(r['branch']['assessment']['recommended'],'pit_now')
        r=self.incident('sc');self.assertEqual(r['observed']['race_control'],'SC')
        report=next(s for s in r['branch']['assessment']['specialists'] if s['role']=='Race Conditions & Incidents')
        self.assertEqual(report['tools']['read_race_control']['state'],'SC')
        r=self.incident('clear_conditions');self.assertEqual(r['observed']['condition'],'dry');self.assertEqual(r['observed']['race_control'],'GREEN')

    def test_rival_feed_contains_no_future_pit_or_stint_end(self):
        feed=RivalFeed(self.rivals)
        pit=next(e for e in self.rivals['events'] if e['kind']=='pit_entry')
        before='2025-07-27T00:00:00.000000+00:00'
        self.assertEqual(feed.snapshot(before,3)['recent_pits'],[])
        snap=feed.snapshot(pit['available_at'],3)
        self.assertTrue(any(p['driver_number']==pit['payload']['driver_number'] and p['event']=='pit_entry' for p in snap['recent_pits']))
        self.assertTrue(all(parse_time(p['at'])<=parse_time(snap['as_of']) for p in snap['recent_pits']))
        self.assertNotIn('lap_end',json.dumps(snap));self.assertNotIn('lane_duration',json.dumps(snap))
        self.assertNotIn(16,{d['driver_number'] for d in snap['drivers']})

    def test_new_safety_car_and_nearby_rival_stop_trigger_monitor(self):
        o=copy.deepcopy(self.run['observed']);b=copy.deepcopy(self.run['branch'])
        o['race_control']='VSC';self.assertIsNone(decision_trigger(b,o))
        b['tyre_age']=30;self.assertEqual(decision_trigger(b,o)['key'],'flag-VSC')
        o['race_control']='GREEN';o['rivals']={'nearby':[{'driver_number':4}], 'recent_pits':[{'event':'pit_entry','driver_number':4,'name':'Lando Norris','at':o['as_of']}]}
        self.assertIn('Lando Norris',decision_trigger(b,o)['reason'])

    def test_dataset_change_cannot_silently_resume_same_run(self):
        other=PitwallService(self.path,self.timeline,client=ResponsesClient(api_key=''),rivals={'drivers':[],'events':[]})
        try:
            with self.assertRaisesRegex(Conflict,'Rival data changed'):other.jump(self.owner,self.rid,21,'mismatch',0)
        finally:other.close()

    def test_pit_exit_green_light_does_not_erase_safety_car_start(self):
        self.assertEqual(self.service.observed(1)['race_control'],'SC')
        self.assertEqual(self.service.observed(4)['race_control'],'SC')
        self.assertEqual(self.service.observed(5)['race_control'],'GREEN')

    def test_analysis_mode_is_saved_for_automatic_incident_assessment(self):
        r=self.service.configure_mode(self.owner,self.rid,'local','mode',0)
        self.assertEqual(r['branch']['director_mode'],'local')
        r=self.incident('puncture')
        self.assertEqual(r['job']['mode'],'local')
        self.assertEqual(r['branch']['assessment']['source'],'local')

    def test_old_runs_are_readable_and_cannot_be_mutated(self):
        with self.service.replay._connect() as db:
            b=copy.deepcopy(self.run['branch']);b.pop('simulation_version');db.execute('UPDATE web_branches SET state=? WHERE run_id=?',(json.dumps(b),self.rid))
        self.assertTrue(self.get()['archived'])
        with self.assertRaisesRegex(Conflict,'earlier simulation'):self.service.assess(self.owner,self.rid,'local','archive',0)

if __name__=='__main__':unittest.main()
