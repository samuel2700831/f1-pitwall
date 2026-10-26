"""Austin playback, direct navigation, clean strategy feed and agent evidence."""
import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from pitwall.data import ROOT,load_timeline,parse_time
from pitwall.service import PitwallService,Conflict
from pitwall.agents import ResponsesClient
from pitwall.simulation import advance_lap,undercut_options,decision_trigger


class AustinTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.timeline=load_timeline(ROOT/'data/austin-2025-demo.json')

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.db=Path(self.temp.name)/'austin.sqlite'
        self.s=PitwallService(self.db,self.timeline,client=ResponsesClient(api_key=''))
        self.r=self.s.create('browser',1,'austin');self.rid=self.r['run_id'];self.serial=0
        self.addCleanup(self.temp.cleanup);self.addCleanup(self.s.close)

    def key(self):self.serial+=1;return 'request-'+str(self.serial)
    def get(self):return self.s.get('browser',self.rid)
    def wait(self):
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            r=self.get()
            if r['job'] and r['job']['status'] not in ('queued','running'):
                self.assertEqual(r['job']['status'],'completed');return r
            time.sleep(.01)
        self.fail('Director job did not finish')

    def jump(self,lap):
        self.s.jump('browser',self.rid,lap,self.key(),self.get()['version'],monitor=False,refresh=True)
        return self.wait()

    def call(self,choice):
        r=self.get()
        if r['branch']['assessment']['status']=='automatic':r=self.s.request_review('browser',self.rid,self.key(),r['version'])
        return self.s.decide('browser',self.rid,choice,'MEDIUM','Explicit human strategy call',self.key(),r['version'])

    def test_race_is_austin_2025_and_all_56_boundaries_exist(self):
        self.assertEqual(self.s.meta()['race']['session_key'],9888);self.assertEqual(self.s.meta()['laps'],56)
        for lap in range(1,57):
            o=self.s.observed(lap);self.assertEqual(o['total_laps'],56)
            self.assertTrue(all(l['lap']<lap for l in o['recent_laps']))

    def test_no_historical_leclerc_pits_or_later_stints_in_web_feed(self):
        events=self.timeline['events']
        self.assertFalse(any(e['kind']=='pit_visit_completed' for e in events))
        self.assertEqual(len([e for e in events if e['kind']=='tyre_stint_started']),1)
        self.assertNotIn('is_pit_out_lap',json.dumps(events))
        self.assertTrue(self.timeline['controlled_strategy_removed'])
        observed=self.s.observed(40)
        self.assertEqual(observed['observed_compound'],self.r['branch']['compound'])
        self.assertEqual(observed['observed_tyre_age'],self.r['branch']['tyre_age']+39)

    def test_queued_not_started_until_start_race(self):
        with patch('pitwall.service.time.time',return_value=100):
            self.assertEqual(self.s.tick('browser',self.rid,1000)['branch']['lap'],1)
            r=self.s.playback('browser',self.rid,'start',80,'start',0)
        self.assertEqual(r['branch']['playback']['status'],'running');self.assertGreater(r['branch']['playback']['next_at'],100)
        self.assertEqual(r['branch']['lap'],1)

    def test_due_tick_runs_one_lap_and_refreshes_all_three_tools(self):
        with patch('pitwall.service.time.time',return_value=100):r=self.s.playback('browser',self.rid,'start',80,'start',0)
        with patch('pitwall.service.time.time',return_value=105):r=self.s.tick('browser',self.rid)
        self.assertEqual(r['branch']['lap'],2);self.assertEqual(r['branch']['pit_count'],0)
        self.assertEqual(r['branch']['telemetry']['lap'],2)
        self.assertEqual(len(r['branch']['telemetry']['reports']),3)
        self.assertTrue(all(e['information'] is not None for e in r['branch']['telemetry']['trace']))
        same=self.s.tick('browser',self.rid,105)
        self.assertEqual(same['version'],r['version'])

    def test_pause_halts_progress_and_start_retry_is_idempotent(self):
        original=self.s.playback('browser',self.rid,'start',40,'start',0)
        self.assertEqual(self.s.playback('browser',self.rid,'start',40,'start',0),original)
        paused=self.s.playback('browser',self.rid,'pause',40,'pause',original['version'])
        self.assertEqual(self.s.tick('browser',self.rid,9999999999),paused)

    def test_automatic_running_pauses_at_first_strategy_call_without_boxing(self):
        r=self.s.playback('browser',self.rid,'start',80,'start',0)
        for lap in range(25):
            with patch('pitwall.service.time.time',return_value=100000000000+lap*100):r=self.s.tick('browser',self.rid)
            if r['branch']['pause']:break
        self.assertIsNotNone(r['branch']['pause']);self.assertLess(r['branch']['lap'],25)
        r=self.wait();self.assertEqual(r['branch']['playback']['status'],'decision')
        self.assertEqual(r['branch']['pit_count'],0)
        approved=self.call('hold');self.assertEqual(approved['branch']['playback']['status'],'running')

    def test_manual_jump_reaches_requested_lap_and_refreshes_every_agent(self):
        r=self.jump(20)
        self.assertEqual(r['branch']['lap'],20);self.assertEqual(len(r['branch']['history']),19)
        self.assertEqual(r['branch']['pit_count'],0);self.assertEqual(r['branch']['compound'],'SOFT')
        self.assertEqual(r['branch']['tyre_age'],22)
        telemetry=r['branch']['telemetry'];self.assertEqual(telemetry['as_of'],r['as_of'])
        self.assertEqual({report['as_of'] for report in telemetry['reports']},{r['as_of']})
        rival=telemetry['reports'][0];self.assertEqual(len(rival['tools']['read_rival_positions']['drivers']),19)
        self.assertIn('screen_undercut',rival['tools'])
        self.assertEqual(r['branch']['assessment']['memory_records_used'],len(r['memory']))

    def test_direct_jump_supersedes_unapproved_advice_and_keeps_saved_intent(self):
        r=self.s.remember('browser',self.rid,'Protect track position','intent',0)
        r=self.jump(15);self.assertEqual(r['branch']['assessment']['status'],'awaiting_human')
        r=self.jump(30);self.assertEqual(r['branch']['lap'],30)
        self.assertEqual(r['branch']['pit_count'],0)
        self.assertTrue(any(m['content']=='Protect track position' for m in r['memory']))

    def test_forward_jump_executes_only_a_human_authorized_stop(self):
        self.jump(15);r=self.call('pit_now')
        r=self.jump(30)
        self.assertEqual(r['branch']['pit_count'],1);self.assertEqual(r['branch']['compound'],'MEDIUM')
        self.assertEqual(r['branch']['tyre_age'],15)
        self.assertEqual(len([h for h in r['branch']['history'] if h['pit']]),1)

    def test_pit_affected_historical_laps_have_no_pace_reference(self):
        raw=json.loads((ROOT/'data/raw/austin-2025/pit.json').read_text()) if (ROOT/'data/raw/austin-2025/pit.json').exists() else []
        for pit in raw:
            if pit['driver_number']==16:
                entry=next(e for e in self.timeline['events'] if e['kind']=='completed_lap' and e['payload']['lap_number']==pit['lap_number'])
                self.assertIsNone(entry['payload']['lap_duration'])

    def test_current_rival_pits_and_tool_results_are_time_gated(self):
        r=self.jump(25);cutoff=parse_time(r['as_of'])
        for pit in r['branch']['telemetry']['reports'][0]['tools']['read_rival_pits']:
            self.assertLessEqual(parse_time(pit['at']),cutoff)
        self.assertNotIn('lap_end',json.dumps(r['branch']['telemetry']))

    def test_vsc_ending_and_track_clear_restore_green(self):
        self.assertEqual(self.s.observed(8)['race_control'],'VSC')
        self.assertEqual(self.s.observed(12)['race_control'],'GREEN')
        self.assertEqual(self.s.observed(40)['race_control'],'GREEN')

    def test_undercut_screen_uses_gap_age_and_current_conditions(self):
        observed=copy.deepcopy(self.r['observed']);b=copy.deepcopy(self.r['branch']);b.update(lap=20,tyre_age=16)
        observed['rivals']['nearby']=[{'driver_number':4,'full_name':'Lando Norris','relative_gap_seconds':-2.1,'tyre_age':15,'compound':'MEDIUM'}]
        options=undercut_options(b,observed)
        self.assertEqual(options[0]['driver_number'],4);self.assertEqual(options[0]['historical_gap_ahead_seconds'],2.1)
        self.assertNotIn('predicted_position',options[0]);self.assertIn('no pass is guaranteed',options[0]['note'])
        observed['condition']='rain';self.assertEqual(undercut_options(b,observed),[])

    def test_tyre_dropoff_triggers_before_cliff(self):
        b=copy.deepcopy(self.r['branch']);b.update(lap=10,tyre_age=12)
        self.assertIsNone(decision_trigger(b,self.r['observed']))  # Warning alone does not pause.
        b['tyre_age']=25
        self.assertIsNotNone(decision_trigger(b,self.r['observed']))

    def test_restart_preserves_race_memory_but_requires_resume(self):
        r=self.s.playback('browser',self.rid,'start',80,'start',0)
        other=PitwallService(self.db,self.timeline,client=ResponsesClient(api_key=''))
        try:
            saved=other.get('browser',self.rid)
            self.assertEqual(saved['branch']['playback']['status'],'interrupted')
            self.assertEqual(saved['memory'],r['memory']);self.assertEqual(saved['branch']['lap'],1)
        finally:other.close()

    def test_final_lap_uses_56_lap_horizon_and_finishes_once(self):
        r=self.s.create('browser',56,'last');rid=r['run_id']
        self.assertEqual(r['branch']['total_laps'],56)
        done=self.s.jump('browser',rid,56,'finish',0,True,monitor=False,refresh=False)
        self.assertTrue(done['branch']['finished']);self.assertEqual(done['branch']['playback']['status'],'finished')
        self.assertEqual(done['branch']['history'][-1]['lap'],56)

    def test_unexpected_director_failure_pauses_clock_and_preserves_car(self):
        r=self.s.playback('browser',self.rid,'start',80,'start',0)
        with patch('pitwall.service.StrategyWorkflow.run',side_effect=RuntimeError('private provider details')):
            self.s.assess('browser',self.rid,'local','failure',r['version'])
            for _ in range(200):
                saved=self.get()
                if saved['job']['status']=='failed':break
                time.sleep(.01)
        self.assertEqual(saved['job']['status'],'failed')
        self.assertEqual(saved['branch']['playback']['status'],'paused')
        self.assertEqual(saved['branch']['pit_count'],0)
        self.assertNotIn('private provider details',saved['job']['error'])
        self.assertEqual(saved['branch']['lap'],1)

    def test_invalid_playback_controls_and_stale_approval_are_rejected(self):
        for action,speed in [('start',True),('start',0),('launch',80)]:
            with self.assertRaises(ValueError):self.s.playback('browser',self.rid,action,speed,self.key(),0)
        r=self.jump(20)
        with self.assertRaises(Conflict):self.s.decide('browser',self.rid,'pit_now','HARD','Stale approval',self.key(),0)

if __name__=='__main__':unittest.main()
