"""Real-data timing calibration, independent of the LLM's strategy choice."""
import copy
import tempfile
import unittest
from pathlib import Path

from pitwall.agents import ResponsesClient
from pitwall.data import ROOT, load_timeline, parse_time
from pitwall.race_model import classification, penalty, stop_cost
from pitwall.service import PitwallService
from pitwall.simulation import advance_lap, new_branch


class AustinTimingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.s=PitwallService(Path(self.temp.name)/'timing.sqlite',
                             load_timeline(ROOT/'data/austin-2025-demo.json'),
                             client=ResponsesClient(api_key=''))
        self.b=new_branch(self.s.observed(1),1)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.s.close)

    def step(self,b=None):
        b=self.b if b is None else b
        observed=self.s._current(b,self.s.replay._lap_cutoff(b['lap'],'start'))
        advance_lap(b,observed,resolved=self.s._resolve_lap(b,observed))

    def reach(self,lap):
        while self.b['lap']<lap:self.step()

    def test_unchanged_starting_stint_tracks_recorded_gap_and_position(self):
        # Until divergence, identical tyres/conditions must reproduce the
        # reference trajectory rather than accumulating a smoothing penalty.
        for lap in range(2,23):
            self.step()
            o=self.s.observed(lap)
            self.assertAlmostEqual(self.b['race_gap_seconds'],o['historical_gap_to_leader'],places=2)
            self.assertEqual(classification(self.b,o)['position'],o['historical_position'])
            self.assertEqual(self.b['pit_count'],0)
        self.assertEqual(self.b['elapsed_delta_seconds'],0)

    def test_completed_lap_is_visible_despite_source_timestamp_jitter(self):
        for lap in range(2,57):
            o=self.s.observed(lap)
            self.assertEqual(o['recent_laps'][-1]['lap'],lap-1)
            self.assertTrue(all(r['lap']<lap and parse_time(r['available_at'])<=parse_time(o['as_of'])
                                for r in o['recent_laps']))

    def test_realized_lap_pace_matches_clock_before_strategy_diverges(self):
        self.reach(22)
        events={e['payload']['lap_number']:e for e in self.s.replay.timeline['events'] if e['kind']=='completed_lap'}
        for h in self.b['history']:
            e=events[h['lap']]
            recorded=(parse_time(e['available_at'])-parse_time(e['payload']['date_start'])).total_seconds()
            self.assertAlmostEqual(h['simulated_lap_seconds'],recorded,places=3)

    def test_lap_15_human_stop_adds_only_stop_cost_and_tyre_difference(self):
        self.reach(15)
        held=copy.deepcopy(self.b)
        o=self.s.observed(15)
        before=classification(self.b,o)
        self.assertEqual(before['position'],2)
        self.assertAlmostEqual(before['own_gap_seconds'],5.054,places=3)
        self.b['queued_pit']={'lap':15,'compound':'MEDIUM','authorized_by':'human','repair_wing':True}
        expected=stop_cost(self.b,o)['total_loss_seconds']+penalty('MEDIUM',0,'dry')-penalty('SOFT',17,'dry')
        self.step();self.step(held)
        self.assertAlmostEqual(self.b['race_gap_seconds']-held['race_gap_seconds'],expected,places=3)
        self.assertEqual(self.b['pit_count'],1)
        self.assertEqual(classification(self.b,self.s.observed(16))['position'],11)

    def test_damage_loss_is_carried_after_repair_not_reset_to_reference(self):
        self.reach(12)
        healthy=copy.deepcopy(self.b)
        self.b['incidents']=[{'id':'wing','type':'front_wing','active':True}]
        for _ in range(3):self.step();self.step(healthy)
        self.assertAlmostEqual(self.b['race_gap_seconds']-healthy['race_gap_seconds'],12,places=3)
        self.b['incidents'][0]['active']=False
        for _ in range(3):self.step();self.step(healthy)
        self.assertAlmostEqual(self.b['race_gap_seconds']-healthy['race_gap_seconds'],12,places=3)

    def test_reference_stop_never_becomes_simulated_stop_or_stop_loss(self):
        self.reach(24)
        self.assertEqual(self.b['pit_count'],0)
        self.assertEqual(self.b['compound'],'SOFT')
        self.assertFalse(any(h['pit'] for h in self.b['history']))
        # Actual clock lap 23 includes a long pit-affected lap. Replacing that
        # clock duration with a clean estimate removes the historical stop.
        reference=self.s.observed(24)['historical_gap_to_leader']
        self.assertLess(self.b['race_gap_seconds'],reference-10)
        self.assertLess(self.b['history'][22]['simulated_lap_seconds'],110)

    def test_unchanged_gap_is_not_a_hardcoded_position(self):
        self.reach(15)
        healthy=copy.deepcopy(self.b)
        self.b['incidents']=[{'id':'puncture','type':'puncture','active':True}]
        self.step();self.step(healthy)
        self.assertAlmostEqual(self.b['race_gap_seconds']-healthy['race_gap_seconds'],25,places=3)
        self.assertGreater(classification(self.b,self.s.observed(16))['position'],
                           classification(healthy,self.s.observed(16))['position'])

    def test_jump_incremental_and_late_entry_share_timing_state(self):
        self.reach(30)
        created=self.s.create('test',1,'start')
        jumped=self.s.jump('test',created['run_id'],30,'jump',0,monitor=False)
        entered=self.s.create('test',30,'late-entry')
        for result in (jumped,entered):
            self.assertAlmostEqual(result['branch']['race_gap_seconds'],self.b['race_gap_seconds'],places=3)
            self.assertAlmostEqual(result['branch']['elapsed_delta_seconds'],self.b['elapsed_delta_seconds'],places=3)
            self.assertEqual(result['branch']['pit_count'],0)
        self.assertEqual(entered['memory'],[])

    def test_restart_preserves_carried_timing_and_human_stop(self):
        r=self.s.create('test',1,'restart')
        r=self.s.jump('test',r['run_id'],15,'jump-15',0,monitor=False)
        # Seed an authorized command, then advance through the same public
        # transaction path used by playback; the branch survives a restart.
        with self.s.replay._connect() as db:
            import json
            b=r['branch'];b['queued_pit']={'lap':15,'compound':'MEDIUM','authorized_by':'human','repair_wing':True}
            db.execute('UPDATE web_branches SET state=? WHERE run_id=?',(json.dumps(b),r['run_id']))
        r=self.s.jump('test',r['run_id'],16,'jump-16',r['version'],monitor=False)
        reopened=PitwallService(self.s.replay.database,self.s.replay.timeline,client=ResponsesClient(api_key=''))
        self.addCleanup(reopened.close)
        saved=reopened.get('test',r['run_id'])
        self.assertEqual(saved['branch']['elapsed_delta_seconds'],r['branch']['elapsed_delta_seconds'])
        later=reopened.jump('test',r['run_id'],17,'jump-17',saved['version'],monitor=False)
        self.assertEqual(later['branch']['pit_count'],1)
        self.assertNotEqual(later['branch']['elapsed_delta_seconds'],0)

    def test_injected_safety_car_does_not_charge_only_leclerc_for_global_slowdown(self):
        self.reach(15)
        self.b['condition_overrides']={'race_control':'SC'}
        self.step()
        self.assertGreaterEqual(self.b['history'][-1]['simulated_lap_seconds'],145)
        self.assertAlmostEqual(self.b['elapsed_delta_seconds'],0,places=3)

    def test_forecast_uses_only_pre_lap_completed_pace(self):
        self.reach(15)
        o=self.s.observed(15)
        self.assertTrue(all(l['lap']<15 for l in o['recent_laps']))
        self.assertLess(o['pace_seconds'],110)
        # Resolving the next lap does not insert its data into the checkpoint.
        self.s._resolve_lap(self.b,o)
        self.assertEqual(o,self.s.observed(15))


if __name__=='__main__':unittest.main()
