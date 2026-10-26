"""Decision-useful position/stop estimates and quiet-lap policy regressions."""
import copy
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from pitwall.data import ROOT,load_timeline
from pitwall.agents import StrategyWorkflow,ResponsesClient
from pitwall.service import PitwallService
from pitwall.simulation import new_branch,advance_lap,decision_trigger
from pitwall.race_model import classification,stop_cost,pit_forecast
from pitwall.specialists import Specialist,ROLE_TOOLS


def fixture():
    drivers=[]
    for number,name,gap,pace in [(1,'VER',0,101),(4,'NOR',3,102),(44,'HAM',8,103),(81,'PIA',15,104),(63,'RUS',28,105)]:
        drivers.append({'driver_number':number,'full_name':name,'name_acronym':name,'position':len(drivers)+1,
                        'gap_to_leader_seconds':gap,'last_lap_seconds':pace,'pace_seconds':pace,
                        'representative_laps':3,'compound':'MEDIUM','tyre_age':15,'relative_gap_seconds':gap-4})
    o={'lap':20,'as_of':'2025-10-19T19:35:00+00:00','total_laps':56,'observed_compound':'MEDIUM','observed_tyre_age':14,
       'historical_position':3,'historical_gap_to_leader':4,'condition':'dry','race_control':'GREEN','pace_seconds':99,
       'weather_age_seconds':30,'issues':[],'evidence_refs':[],
       'recent_laps':[{'lap':i,'seconds':100} for i in (17,18,19)],
       'rivals':{'drivers':drivers,'nearby':drivers[:3],'recent_pits':[],'available':True}}
    return o,new_branch(o,20)


class PositionForecastTests(unittest.TestCase):
    def setUp(self):self.o,self.b=fixture()

    def test_current_position_and_neighbor_gap_are_calculated(self):
        order=classification(self.b,self.o)
        self.assertEqual(order['position'],3);self.assertEqual(order['ahead']['name_acronym'],'NOR')
        self.assertEqual(order['gap_to_ahead_seconds'],1);self.assertEqual(order['gap_to_behind_seconds'],4)
        self.assertEqual(order['quality'],'partial')

    def test_pit_exit_position_neighbors_and_loss_are_calculated(self):
        f=pit_forecast(self.b,self.o)
        self.assertEqual(f['pit_exit_position'],6);self.assertEqual(f['positions_lost'],3)
        self.assertEqual(f['rejoin_behind']['name_acronym'],'RUS');self.assertEqual(f['gap_behind_seconds'],1)
        self.assertEqual(f['stop_cost']['total_loss_seconds'],25)

    def test_vsc_and_sc_have_distinct_transit_loss_and_same_service(self):
        profiles={flag:stop_cost(self.b,{**self.o,'race_control':flag}) for flag in ('GREEN','VSC','SC')}
        self.assertGreater(profiles['GREEN']['total_loss_seconds'],profiles['VSC']['total_loss_seconds'])
        self.assertGreater(profiles['VSC']['total_loss_seconds'],profiles['SC']['total_loss_seconds'])
        self.assertEqual({p['stationary_seconds'] for p in profiles.values()},{2.5})

    def test_wing_and_puncture_service_costs_are_separate(self):
        plain=stop_cost(self.b,self.o)
        self.b['incidents']=[{'id':'wing','type':'front_wing','active':True}]
        wing=stop_cost(self.b,self.o);self.assertEqual(wing['total_loss_seconds']-plain['total_loss_seconds'],5)
        self.assertEqual(stop_cost(self.b,self.o,False)['total_loss_seconds'],plain['total_loss_seconds'])
        self.b['incidents'].append({'id':'puncture','type':'puncture','active':True})
        both=stop_cost(self.b,self.o);self.assertEqual(both['total_loss_seconds']-wing['total_loss_seconds'],1.5)
        self.assertEqual(both['puncture_checks_seconds'],1.5)

    def test_carried_race_gap_is_not_overwritten_by_historical_leclerc(self):
        self.o['historical_gap_to_leader']=999
        initial=self.b['race_gap_seconds'];advance_lap(self.b,self.o)
        self.assertLess(abs(self.b['race_gap_seconds']-initial),10)
        self.assertNotEqual(self.b['race_gap_seconds'],999)

    def test_human_pit_updates_carried_gap_by_modeled_loss_and_tyre_change(self):
        held=copy.deepcopy(self.b);pitted=copy.deepcopy(self.b)
        pitted['queued_pit']={'lap':20,'compound':'MEDIUM','authorized_by':'human','repair_wing':True}
        advance_lap(held,self.o);advance_lap(pitted,self.o)
        self.assertTrue(pitted['history'][-1]['pit']);self.assertEqual(pitted['history'][-1]['stop_cost']['total_loss_seconds'],25)
        self.assertGreater(pitted['race_gap_seconds'],held['race_gap_seconds'])
        self.assertEqual(pitted['pit_count'],1)

    def test_fresh_compound_changes_pace_and_recovery_estimates(self):
        soft=pit_forecast(self.b,self.o,'SOFT');hard=pit_forecast(self.b,self.o,'HARD')
        self.assertLess(soft['fresh_tyre_pace_seconds'],hard['fresh_tyre_pace_seconds'])
        self.assertGreaterEqual(soft['potential_overtakes'],hard['potential_overtakes'])

    def test_recovery_uses_measured_pace_and_exposes_catch_assumptions(self):
        f=pit_forecast(self.b,self.o)
        self.assertGreater(f['potential_overtakes'],0)
        self.assertEqual(f['potential_overtakes'],len(f['potential_targets']))
        self.assertTrue(all(1<=t['estimated_catch_in_laps']<=8 for t in f['potential_targets']))
        self.assertIn('may prevent passes',f['note'])
        self.assertNotIn('guaranteed_overtakes',f)

    def test_recovery_accounts_for_cumulative_passing_time(self):
        f=pit_forecast(self.b,self.o)
        targets=f['potential_targets']
        self.assertEqual([t['estimated_catch_in_laps'] for t in targets],sorted(t['estimated_catch_in_laps'] for t in targets))
        for index,target in enumerate(targets,1):
            self.assertGreaterEqual(target['modeled_gain_in_horizon_seconds'],target['gap_after_pit_seconds']+index-.02)

    def test_stale_stopped_driver_does_not_keep_a_frozen_front_position(self):
        self.o['rivals']['drivers'][0]['last_lap_at']='2025-10-19T19:00:00+00:00'
        order=classification(self.b,self.o)
        stopped=next(r for r in order['field'] if r['driver_number']==1)
        self.assertGreater(stopped['race_gap_seconds'],1000)
        self.assertIn('stale',stopped['timing_status'])
        self.assertLess(order['position'],3)

    def test_no_passing_forecast_under_sc_vsc_or_red(self):
        for flag in ('SC','VSC','RED'):
            f=pit_forecast(self.b,{**self.o,'race_control':flag})
            self.assertEqual(f['potential_overtakes'],0)
            if flag=='RED':self.assertFalse(f['available']);self.assertIsNone(f['pit_exit_position'])

    def test_unknown_rival_pace_does_not_create_catch_predictions(self):
        for rival in self.o['rivals']['drivers']:rival['pace_seconds']=None;rival['last_lap_seconds']=None
        self.assertEqual(pit_forecast(self.b,self.o)['potential_overtakes'],0)

    def test_leclerc_specialist_exposes_cost_position_and_recovery_to_director(self):
        report=Specialist('Leclerc Performance',self.o,self.b,lambda *args:None).run()
        values=report['tools']['evaluate_pit_consequences']
        for key in ('current_position','pit_exit_position','rejoin_behind','stop_cost','potential_targets'):
            self.assertIn(key,values)
        self.assertEqual(report['tools']['read_simulated_telemetry']['simulated_position'],3)

    def test_unchanged_flag_and_saved_note_do_not_force_hold_review(self):
        self.b['tyre_age']=1;self.o['race_control']='VSC';self.o['issues']=['VSC conditions'];self.b['pause']={'reason':'Review VSC'}
        memory=[{'kind':'human_override','content':'Avoid unnecessary stops'}]
        a=StrategyWorkflow(self.o,self.b,memory,client=ResponsesClient(api_key='')).run()
        self.assertEqual(a['recommended'],'hold');self.assertEqual(a['status'],'automatic')
        self.assertIsNone(decision_trigger(self.b,self.o))

    def test_acknowledged_unchanged_trigger_does_not_repeat_after_three_laps(self):
        self.b['tyre_age']=28
        trigger=decision_trigger(self.b,self.o);self.assertIsNotNone(trigger)
        self.b['acknowledged'][trigger['key']]=20
        self.b['trigger_state'][trigger['key']]={'band':trigger['band'],'lap':20}
        # Acknowledging all current reasons models a human stay-out instruction.
        for _ in range(12):
            t=decision_trigger(self.b,self.o)
            if not t:break
            self.b['acknowledged'][t['key']]=20;self.b['trigger_state'][t['key']]={'band':t['band'],'lap':20}
        self.b['lap']=23
        self.assertIsNone(decision_trigger(self.b,self.o))


class RaceViewTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.s=PitwallService(Path(self.temp.name)/'race.sqlite',load_timeline(ROOT/'data/austin-2025-demo.json'),client=ResponsesClient(api_key=''))
        self.r=self.s.create('browser',1,'new');self.rid=self.r['run_id']
        self.addCleanup(self.temp.cleanup);self.addCleanup(self.s.close)

    def test_one_two_three_times_have_proportional_durations(self):
        for speed in (1,2,3):
            with patch('pitwall.service.time.time',return_value=100):
                r=self.s.playback('browser',self.rid,'speed',speed,'speed-'+str(speed),self.s.get('browser',self.rid)['version'])
                delay=self.s._lap_delay(r['branch'])
            self.assertAlmostEqual(delay*speed,100-.6+.16*3,places=2)

    def test_direct_jump_starts_new_lap_without_old_partial_progress(self):
        with patch('pitwall.service.time.time',return_value=100):r=self.s.playback('browser',self.rid,'start',1,'start',0)
        with patch('pitwall.service.time.time',return_value=125):r=self.s.playback('browser',self.rid,'pause',1,'pause',r['version'])
        self.assertGreater(r['branch']['playback']['progress_fraction'],.2)
        jumped=self.s.jump('browser',self.rid,3,'jump',r['version'],monitor=False)
        self.assertEqual(jumped['branch']['playback']['progress_fraction'],0)

    def test_all_three_tool_snapshots_are_timestamped_and_change_each_lap(self):
        first=self.r['branch']['telemetry']
        next=self.s.jump('browser',self.rid,2,'next',0,monitor=False)
        latest=next['branch']['telemetry']
        self.assertNotEqual(first['snapshot_id'],latest['snapshot_id']);self.assertEqual(latest['lap'],2)
        self.assertEqual(len(latest['trace']),sum(len(tools) for tools in ROLE_TOOLS.values()))
        self.assertTrue(all(event['called_at'] and event['information'] is not None for event in latest['trace']))
        self.assertEqual({report['lap'] for report in latest['reports']},{2})

    def test_selected_repair_preview_is_read_only_and_changes_cost(self):
        b=copy.deepcopy(self.r['branch']);b['incidents']=[{'id':'wing','type':'front_wing','active':True}]
        with self.s.replay._connect() as db:db.execute('UPDATE web_branches SET state=? WHERE run_id=?',(json.dumps(b),self.rid))
        before=self.s.get('browser',self.rid)
        repaired=self.s.preview('browser',self.rid,'MEDIUM',True)
        unrepaired=self.s.preview('browser',self.rid,'MEDIUM',False)
        self.assertEqual(repaired['forecast']['stop_cost']['total_loss_seconds']-unrepaired['forecast']['stop_cost']['total_loss_seconds'],5)
        self.assertEqual(before,self.s.get('browser',self.rid))

    def test_completed_job_cannot_be_returned_with_old_branch_snapshot(self):
        with self.s.replay._connect() as db:
            db.execute("INSERT INTO web_jobs (job_id,run_id,status,source_version,mode,trace,created_at,updated_at) VALUES ('race',?,'queued',0,'local','[]','a','a')",(self.rid,))
        entered,updated=threading.Event(),threading.Event();original=self.s._view
        def writer():
            entered.wait(2)
            with self.s.replay._connect() as db:
                db.execute('BEGIN IMMEDIATE');b=copy.deepcopy(self.r['branch']);b['assessment']={'status':'automatic'}
                db.execute('UPDATE web_branches SET state=? WHERE run_id=?',(json.dumps(b),self.rid))
                db.execute("UPDATE web_jobs SET status='completed' WHERE job_id='race'");updated.set()
        thread=threading.Thread(target=writer);thread.start()
        def view(db,row,record,branch):
            entered.set();self.assertTrue(updated.wait(2));return original(db,row,record,branch)
        with patch.object(self.s,'_view',side_effect=view):snapshot=self.s.get('browser',self.rid)
        thread.join(3)
        self.assertEqual(snapshot['job']['status'],'queued');self.assertIsNone(snapshot['branch']['assessment'])
        self.assertEqual(self.s.get('browser',self.rid)['job']['status'],'completed')

if __name__=='__main__':unittest.main()
