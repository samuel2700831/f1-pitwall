import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from pitwall.agents import ResponsesClient,StrategyWorkflow
from pitwall.data import ROOT,load_timeline
from pitwall.service import PitwallService,Conflict


class RaceControlsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.s=PitwallService(Path(self.temp.name)/'controls.sqlite',load_timeline(ROOT/'data/austin-2025-demo.json'),client=ResponsesClient(api_key=''))
        self.r=self.s.create('driver',1,'start')
        self.addCleanup(self.temp.cleanup);self.addCleanup(self.s.close)

    def test_stop_halts_clock_and_blocks_resume_or_further_actions(self):
        started=self.s.playback('driver',self.r['run_id'],'start',80,'running',0)
        stopped=self.s.playback('driver',self.r['run_id'],'stop',80,'stop',started['version'])
        self.assertEqual(stopped['branch']['playback']['status'],'stopped')
        self.assertEqual(self.s.tick('driver',self.r['run_id'],10**12),stopped)
        self.assertEqual(self.s.playback('driver',self.r['run_id'],'stop',80,'stop',started['version']),stopped)
        with self.assertRaises(Conflict):self.s.playback('driver',self.r['run_id'],'start',80,'resume',stopped['version'])
        with self.assertRaises(Conflict):self.s.jump('driver',self.r['run_id'],20,'jump',stopped['version'])

    def test_stop_cancels_authorized_pit_and_reset_opens_clean_isolated_run(self):
        with self.s.replay._connect() as db:
            b=self.r['branch'];b['queued_pit']={'lap':1,'compound':'MEDIUM','authorized_by':'human','repair_wing':True}
            b['incidents']=[{'id':'damage','type':'puncture','active':True}]
            db.execute('UPDATE web_branches SET state=? WHERE run_id=?',(json.dumps(b),self.r['run_id']))
        stopped=self.s.playback('driver',self.r['run_id'],'stop',1,'stop',0)
        self.assertIsNone(stopped['branch']['queued_pit']);self.assertEqual(stopped['branch']['pit_count'],0)
        fresh=self.s.create('driver',1,'reset')
        self.assertEqual(fresh['branch']['playback']['status'],'ready');self.assertEqual(fresh['branch']['lap'],1)
        self.assertEqual(fresh['branch']['incidents'],[]);self.assertEqual(fresh['memory'],[])
        self.assertNotEqual(fresh['run_id'],stopped['run_id'])
        self.assertEqual(self.s.get('driver',stopped['run_id']),stopped)

    def test_queued_assessment_is_cancelled_before_calling_provider(self):
        with patch.object(self.s.executor,'submit'):
            pending=self.s.assess('driver',self.r['run_id'],'local','assess',0)
        stopped=self.s.playback('driver',self.r['run_id'],'stop',1,'stop',pending['version'])
        with patch('pitwall.service.StrategyWorkflow.run',side_effect=AssertionError('Cancelled job must not run')):
            self.s._work('driver',self.r['run_id'],pending['job']['job_id'])
        self.assertEqual(self.s.get('driver',self.r['run_id']),stopped)
        self.assertEqual(stopped['job']['status'],'cancelled')

    def test_inflight_assessment_cannot_revive_a_stopped_race(self):
        entered=threading.Event();release=threading.Event();original=StrategyWorkflow.run
        def slow(workflow,mode):
            result=original(workflow,mode);entered.set();release.wait(3);return result
        with patch.object(self.s.executor,'submit'):
            pending=self.s.assess('driver',self.r['run_id'],'local','assess',0)
        with patch('pitwall.service.StrategyWorkflow.run',slow):
            worker=threading.Thread(target=self.s._work,args=('driver',self.r['run_id'],pending['job']['job_id']))
            worker.start()
            try:
                self.assertTrue(entered.wait(3))
                stopped=self.s.playback('driver',self.r['run_id'],'stop',1,'stop',pending['version'])
            finally:release.set();worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.s.get('driver',self.r['run_id']),stopped)


if __name__=='__main__':unittest.main()
