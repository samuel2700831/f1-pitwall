import copy
import json
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path

from pitwall.agents import ResponsesClient, ModelError
from pitwall.data import ROOT, load_timeline
from pitwall.evaluation import evaluate, CASES, TARGETS
from pitwall.service import PitwallService, Conflict


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.client=ResponsesClient(api_key='')
        self.s=PitwallService(Path(self.temp.name)/'tests.sqlite',load_timeline(ROOT/'data/austin-2025-demo.json'),client=self.client)
        self.r=self.s.create('owner',15,'evaluation')
        self.addCleanup(self.temp.cleanup);self.addCleanup(self.s.close)

    def evaluate(self,**kwargs):
        return evaluate(self.r['observed'],self.r['branch'],self.r['memory'],client=self.client,**kwargs)

    def wait(self,test):
        deadline=time.monotonic()+4
        while time.monotonic()<deadline:
            test=self.s.get_agent_test('owner',test['test_id'])
            if test['status'] not in ('queued','running'):return test
            time.sleep(.01)
        self.fail('Test worker timed out')

    def model(self,choice):
        calls=0
        def transport(payload):
            nonlocal calls
            calls+=1
            tools=[('get_race_snapshot',{}),('recall_memory',{}),('delegate_rival_analyst',{}),('delegate_conditions_agent',{}),('delegate_leclerc_agent',{}),('compare_strategies',{'stress':'baseline'})] if calls==1 else [('propose_decision',{'choice':choice,'rationale':'Test model supplied this proposal.','needs_human':choice!='hold'})]
            return {'output':[{'type':'function_call','call_id':str(calls)+'-'+str(i),'name':name,'arguments':json.dumps(args)} for i,(name,args) in enumerate(tools)]}
        return ResponsesClient(transport=transport)

    def test_all_local_scenarios_and_each_agent(self):
        for case in CASES:
            for target in TARGETS:
                with self.subTest(case=case['id'],target=target):
                    result=self.evaluate(case=case['id'],target=target)
                    self.assertEqual(result['verdict'],'pass',[c for c in result['checks'] if not c['passed']])
                    self.assertEqual(len(result['reports']),3 if target in ('all','Strategy Director') else 1)

    def test_scenarios_do_not_mutate_original_checkpoint_or_memory(self):
        before=copy.deepcopy(self.r)
        self.evaluate(case='puncture');self.evaluate(case='rain');self.evaluate(case='fresh')
        self.assertEqual(self.r,before)
        self.assertEqual(self.s.get('owner',self.r['run_id']),before)

    def test_scenario_overrides_are_labelled_and_weather_is_consistent(self):
        result=self.evaluate(case='rain')
        observed=result['inputs']['observed']
        self.assertEqual(observed['condition'],'rain');self.assertEqual(observed['rainfall'],1)
        self.assertEqual(observed['simulated_overrides']['condition'],'rain')

    def test_bad_original_puncture_answer_fails_even_when_policy_corrects_it(self):
        self.client=self.model('hold')
        result=self.evaluate(case='puncture',mode='llm')
        self.assertEqual(result['decision']['model_proposal']['choice'],'hold')
        self.assertEqual(result['decision']['recommended'],'pit_now')
        self.assertEqual(result['verdict'],'fail')
        self.assertFalse(next(c for c in result['checks'] if c['name']=='Original proposal handles puncture')['passed'])
        self.assertEqual(len(result['model_requests']),2)
        self.assertIn('get_race_snapshot',[c['tool'] for c in result['trace'] if c.get('information')])

    def test_correct_original_puncture_answer_passes_without_execution(self):
        self.client=self.model('pit_now')
        result=self.evaluate(case='puncture',mode='llm')
        self.assertEqual(result['verdict'],'pass')
        self.assertEqual(result['inputs']['car']['pit_count'],0)
        self.assertIsNone(result['inputs']['car']['queued_pit'])

    def test_red_flag_bad_original_answer_is_visible_and_policy_explains_suspension(self):
        self.client=self.model('hold')
        result=self.evaluate(case='red_flag',mode='llm')
        self.assertFalse(result['decision']['model_proposal']['needs_human'])
        self.assertTrue(result['decision']['needs_human'])
        self.assertIn('Race suspended',result['decision']['rationale'])
        self.assertEqual(result['verdict'],'fail')

    def test_rain_on_slicks_bad_original_hold_fails_even_after_guard(self):
        self.client=self.model('hold')
        result=self.evaluate(case='rain',mode='llm')
        self.assertEqual(result['decision']['model_proposal']['choice'],'hold')
        self.assertEqual(result['decision']['recommended'],'pit_now')
        self.assertTrue(result['decision']['needs_human'])
        self.assertEqual(result['verdict'],'fail')

    def test_safety_car_material_tradeoff_cannot_be_an_automatic_model_hold(self):
        self.client=self.model('hold')
        result=self.evaluate(case='sc',mode='llm')
        self.assertTrue(result['decision']['needs_human'])
        self.assertEqual(result['verdict'],'fail')

    def test_provider_failure_is_a_failed_llm_check_with_visible_recovery(self):
        def fail(_):raise ModelError('Test provider unavailable')
        self.client=ResponsesClient(transport=fail)
        result=self.evaluate(case='puncture',mode='llm')
        self.assertEqual(result['verdict'],'fail')
        self.assertEqual(result['decision']['source'],'local_recovery')
        self.assertIsNone(result['decision']['model_proposal'])

    def test_single_specialist_does_not_call_an_llm(self):
        def forbidden(_):raise AssertionError('A specialist cannot use LLM quota')
        self.client=ResponsesClient(transport=forbidden)
        result=self.evaluate(target='Rival Analyst',mode='llm')
        self.assertEqual(result['verdict'],'pass');self.assertIsNone(result['decision']);self.assertEqual(result['model_requests'],[])

    def test_future_rival_data_fails_the_contract(self):
        self.r['observed']['rivals']['drivers'][0]['timing_at']='2099-01-01T00:00:00Z'
        result=self.evaluate(target='Rival Analyst')
        self.assertEqual(result['verdict'],'fail')

    def test_async_jobs_are_owner_scoped_idempotent_and_leave_race_unchanged(self):
        before=self.s.get('owner',self.r['run_id'])
        first=self.s.test_agents('owner',self.r['run_id'],'async',0,'puncture','all','local')
        done=self.wait(first);self.assertEqual(done['status'],'completed');self.assertEqual(done['result']['verdict'],'pass')
        again=self.s.test_agents('owner',self.r['run_id'],'async',0,'puncture','all','local')
        self.assertEqual(again['test_id'],first['test_id'])
        self.assertEqual(self.s.get('owner',self.r['run_id']),before)
        with self.assertRaises(KeyError):self.s.get_agent_test('other',first['test_id'])
        with self.assertRaises(Conflict):self.s.test_agents('owner',self.r['run_id'],'async',0,'rain')

    def test_stale_checkpoint_and_invalid_inputs_are_rejected(self):
        with self.assertRaises(Conflict):self.s.test_agents('owner',self.r['run_id'],'stale',9)
        for case,target,mode in [('invalid','all','local'),('current','secret','local'),('current','all','other')]:
            with self.assertRaises(ValueError):self.s.test_agents('owner',self.r['run_id'],'invalid',0,case,target,mode)
        with self.assertRaises(ValueError):self.s.test_agents('owner',self.r['run_id'],'missing-llm',0,mode='llm')

    def test_restart_preserves_completed_test_reports(self):
        test=self.wait(self.s.test_agents('owner',self.r['run_id'],'restart',0,'front_wing'))
        reopened=PitwallService(self.s.replay.database,self.s.replay.timeline,client=self.client)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.get_agent_test('owner',test['test_id']),test)

    def test_checkpoint_is_frozen_even_when_race_memory_changes_during_test(self):
        with patch.object(self.s.executor,'submit'):
            test=self.s.test_agents('owner',self.r['run_id'],'frozen',0)
            with self.assertRaises(Conflict):self.s.test_agents('owner',self.r['run_id'],'second',0)
            self.s.remember('owner',self.r['run_id'],'New intent after the test started','intent',0)
        self.s._test_work('owner',test['test_id'])
        done=self.s.get_agent_test('owner',test['test_id'])
        self.assertEqual(done['status'],'completed');self.assertEqual(done['source_version'],0)
        self.assertEqual(done['result']['inputs']['memory'],[])
        self.assertEqual(len(self.s.get('owner',self.r['run_id'])['memory']),1)


if __name__=='__main__':unittest.main()
