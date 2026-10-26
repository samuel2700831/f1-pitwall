import copy
import io
import json
import tempfile
import threading
import time
import unittest
import urllib.error
from unittest.mock import patch, MagicMock
from pathlib import Path

from pitwall.agents import ModelError, ResponsesClient, StrategyWorkflow
from pitwall.data import ROOT, load_timeline, parse_time
from pitwall.service import Conflict, PitwallService
from pitwall.simulation import advance_lap, challenge, compare, new_branch, penalty


class ApplicationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.timeline = load_timeline(ROOT / 'data' / 'spa-2025-demo.json')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / 'web.sqlite3'
        self.service = PitwallService(self.db, self.timeline, client=ResponsesClient(api_key=''))
        self.owner = 'browser-one'
        self.run = self.service.create(self.owner, 20, 'initial')
        self.rid = self.run['run_id']
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.service.close)

    def get(self):
        return self.service.get(self.owner, self.rid)

    def wait(self):
        end = time.monotonic() + 5
        while time.monotonic() < end:
            result = self.get()
            if result['job']['status'] not in ('queued', 'running'):
                return result
            time.sleep(.01)
        self.fail('Assessment did not finish')

    def assess(self, name='assessment'):
        self.service.assess(self.owner, self.rid, 'local', name, self.get()['version'])
        return self.wait()

    def manual(self):
        result = self.assess()
        if result['branch']['assessment']['status'] == 'automatic':
            result = self.service.request_review(self.owner, self.rid, 'review', result['version'])
        return result

    def pit(self, choice='pit_now', compound='HARD'):
        result = self.manual()
        return self.service.decide(self.owner, self.rid, choice, compound, 'Test a different stint', 'pit-choice', result['version'])

    def test_initial_branch_is_seeded_from_observed_tyres(self):
        self.assertEqual(self.run['branch']['compound'], self.run['observed']['observed_compound'])
        self.assertEqual(self.run['branch']['tyre_age'], self.run['observed']['observed_tyre_age'])
        self.assertEqual(self.run['branch']['delta_seconds'], 0)
        self.assertEqual(self.run['memory'], [])

    def test_creation_retry_is_idempotent_and_conflicting_content_rejected(self):
        self.assertEqual(self.service.create(self.owner, 20, 'initial'), self.run)
        with self.assertRaises(Conflict):
            self.service.create(self.owner, 21, 'initial')
        self.assertEqual(len(self.service.list_runs(self.owner)), 1)

    def test_browser_sessions_cannot_read_modify_or_export_other_runs(self):
        self.assertEqual(self.service.list_runs('other-browser'), [])
        for callback in (lambda: self.service.get('other-browser', self.rid),
                         lambda: self.service.export('other-browser', self.rid),
                         lambda: self.service.remember('other-browser', self.rid, 'Intrusion', 'bad', 0)):
            with self.assertRaises(KeyError):
                callback()

    def test_local_assessment_has_real_tools_and_no_llm_claim(self):
        result = self.assess()
        assessment = result['branch']['assessment']
        self.assertEqual(assessment['source'], 'local')
        self.assertIsNone(assessment['model'])
        self.assertEqual(result['job']['status'], 'completed')
        self.assertTrue({'delegate_rival_analyst','delegate_conditions_agent','delegate_leclerc_agent'}.issubset({e['tool'] for e in result['job']['trace']}))
        self.assertGreater(len(assessment['comparison']['options']), 1)

    def test_routine_hold_is_automatic_and_repeat_assessment_remains_routine(self):
        result = self.assess()
        self.assertEqual(result['branch']['assessment']['status'], 'automatic')
        result = self.assess('again')
        self.assertEqual(result['branch']['assessment']['status'], 'automatic')

    def test_saved_human_instruction_is_recalled_without_routine_pause(self):
        self.service.remember(self.owner, self.rid, 'Avoid an extra stop', 'note', 0)
        result = self.assess()
        self.assertEqual(result['branch']['assessment']['memory_records_used'], 1)
        self.assertEqual(result['branch']['assessment']['status'], 'automatic')
        self.assertIn('Saved human context', ' '.join(result['branch']['assessment']['reasons']))

    def test_forward_jump_keeps_memory_and_approved_pit_effect(self):
        result = self.pit()
        result = self.service.jump(self.owner, self.rid, 30, 'forward', result['version'])
        self.assertEqual(result['branch']['compound'], 'HARD')
        self.assertEqual(result['branch']['tyre_age'], 10)
        self.assertEqual(result['branch']['pit_count'], 1)
        self.assertEqual(result['branch']['history'][0]['lap'], 20)
        self.assertTrue(result['branch']['history'][0]['pit'])
        self.assertIsNone(result['branch']['queued_pit'])
        self.assertNotEqual(result['branch']['delta_seconds'], 0)
        self.assertTrue(any('Test a different stint' in m['content'] for m in result['memory']))
        # Actual later historical tyres must not overwrite the user's hard set.
        self.assertEqual(result['observed']['observed_compound'], 'MEDIUM')

    def test_delayed_stop_waits_for_its_lap_and_executes_once(self):
        result = self.pit('pit_in_2')
        result = self.service.jump(self.owner, self.rid, 22, 'before-stop', result['version'])
        self.assertEqual(result['branch']['pit_count'], 0)
        self.assertIsNotNone(result['branch']['queued_pit'])
        result = self.service.jump(self.owner, self.rid, 23, 'after-stop', result['version'])
        self.assertEqual(result['branch']['pit_count'], 1)
        result = self.service.jump(self.owner, self.rid, 30, 'later', result['version'])
        self.assertEqual(result['branch']['pit_count'], 1)

    def test_retry_does_not_duplicate_a_pit_or_memory(self):
        result = self.pit()
        before = result['version']
        memory_count = len(result['memory'])
        original = self.service.jump(self.owner, self.rid, 30, 'forward', before)
        self.assertEqual(self.service.jump(self.owner, self.rid, 30, 'forward', before), original)
        self.assertEqual(self.get()['branch']['pit_count'], 1)
        self.assertEqual(len(self.get()['memory']), memory_count+1)  # execution record, once
        with self.assertRaises(Conflict):
            self.service.jump(self.owner, self.rid, 31, 'forward', original['version'])

    def test_stepwise_and_instant_jump_have_identical_simulated_effect(self):
        result = self.pit('pit_in_2')
        initial = copy.deepcopy(result['branch'])
        for lap in range(20, 30):
            advance_lap(initial, self.service.observed(lap))
        jumped = self.service.jump(self.owner, self.rid, 30, 'forward', result['version'])['branch']
        for key in ('compound', 'tyre_age', 'pit_count', 'delta_seconds', 'queued_pit', 'history'):
            self.assertEqual(jumped[key], initial[key])

    def test_fresh_backward_scenario_has_no_later_memory(self):
        result = self.pit()
        before = self.service.jump(self.owner, self.rid, 30, 'forward', result['version'])
        with self.assertRaises(Conflict):
            self.service.jump(self.owner, self.rid, 10, 'back', before['version'])
        fresh = self.service.create(self.owner, 10, 'fresh')
        self.assertEqual(fresh['memory'], [])
        self.assertEqual(fresh['branch']['history'], [])
        self.assertEqual(self.get(), before)

    def test_pending_human_decision_blocks_advancement(self):
        result = self.manual()
        with self.assertRaisesRegex(Conflict, 'pending'):
            self.service.jump(self.owner, self.rid, 22, 'blocked', result['version'])

    def test_human_choice_requires_current_assessment_and_rationale(self):
        with self.assertRaises(Conflict):
            self.service.decide(self.owner, self.rid, 'pit_now', 'MEDIUM', 'x', 'early', 0)
        result = self.manual()
        for choice, compound, rationale in [('illegal', 'MEDIUM', 'x'), ('pit_now', 'MAGIC', 'x'), ('hold', 'MEDIUM', '')]:
            with self.assertRaises(ValueError):
                self.service.decide(self.owner, self.rid, choice, compound, rationale, 'bad', result['version'])

    def test_stale_version_cannot_approve_outdated_advice(self):
        result = self.manual()
        self.service.remember(self.owner, self.rid, 'New instruction', 'new-input', result['version'])
        with self.assertRaises(Conflict):
            self.service.decide(self.owner, self.rid, 'hold', 'MEDIUM', 'Old advice', 'stale', result['version'])
        self.assertIsNone(self.get()['branch']['assessment'])

    def test_job_retry_returns_same_job_without_duplicate_worker(self):
        result = self.service.assess(self.owner, self.rid, 'local', 'same-job', 0)
        retry = self.service.assess(self.owner, self.rid, 'local', 'same-job', 0)
        self.assertEqual(result['job']['job_id'], retry['job']['job_id'])
        self.wait()
        with self.service.replay._connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM web_jobs').fetchone()[0], 1)

    def test_restart_preserves_tyres_history_and_memory(self):
        result = self.pit()
        result = self.service.jump(self.owner, self.rid, 30, 'jump', result['version'])
        other = PitwallService(self.db, self.timeline)
        try:
            self.assertEqual(other.get(self.owner, self.rid), result)
        finally:
            other.close()

    def test_restart_marks_unfinished_job_interrupted_without_changing_branch(self):
        with self.service.replay._connect() as db:
            db.execute("INSERT INTO web_jobs (job_id,run_id,status,source_version,mode,trace,error,created_at,updated_at,source_lap) VALUES ('abandoned',?,'running',0,'local','[]',NULL,'a','a',20)", (self.rid,))
        other = PitwallService(self.db, self.timeline)
        try:
            result = other.get(self.owner, self.rid)
            self.assertEqual(result['job']['status'], 'interrupted')
            self.assertEqual(result['branch'], self.run['branch'])
        finally:
            other.close()

    def test_changed_input_during_assessment_discards_old_result(self):
        started, release = threading.Event(), threading.Event()
        def transport(_payload):
            started.set();release.wait(3)
            raise ModelError('Injected transient failure')
        self.service.client = ResponsesClient(transport=transport)
        queued = self.service.assess(self.owner, self.rid, 'llm', 'slow', 0)
        self.assertTrue(started.wait(2))
        self.service.remember(self.owner, self.rid, 'New intent during analysis', 'new-note', queued['version'])
        release.set();result = self.wait()
        self.assertEqual(result['job']['status'], 'stale')
        self.assertIsNone(result['branch']['assessment'])
        self.assertEqual(len(result['memory']), 1)

    def test_finish_processes_final_lap_once(self):
        result = self.service.create(self.owner,44,'final')
        rid=result['run_id']
        end = self.service.jump(self.owner,rid,44,'finish',result['version'],True)
        self.assertTrue(end['branch']['finished'])
        self.assertEqual(len(end['branch']['history']),1)
        self.assertEqual(end['branch']['history'][-1]['lap'],44)
        self.assertEqual(self.service.jump(self.owner,rid,44,'finish',result['version'],True),end)
        with self.assertRaises(Conflict):
            self.service.jump(self.owner,rid,44,'again',end['version'],True)

    def test_end_of_race_reduces_forecast_horizon(self):
        result = self.service.create(self.owner, 44, 'last-lap')
        report = compare(result['branch'], result['observed'])
        self.assertEqual(report['horizon_laps'], 1)
        self.assertNotIn('pit_in_2', {o['id'] for o in report['options']})

    def test_all_44_scenarios_load_without_exposing_current_lap_result(self):
        for lap in range(1, 45):
            observed = self.service.observed(lap)
            self.assertTrue(all(item['lap'] < lap for item in observed['recent_laps']))
            cutoff = parse_time(observed['as_of'])
            self.assertTrue(all(parse_time(ref['available_at']) <= cutoff for ref in observed['evidence_refs']))
            report = challenge(new_branch(observed, lap), observed)
            self.assertGreaterEqual(report['comparison']['horizon_laps'], 1)

    def test_forecast_changes_with_age_and_does_not_claim_position(self):
        younger = copy.deepcopy(self.run['branch']);older = copy.deepcopy(younger);older['tyre_age'] = 35
        young = challenge(younger, self.run['observed']);old = challenge(older, self.run['observed'])
        self.assertNotEqual(young['comparison']['options'], old['comparison']['options'])
        self.assertNotIn('finishing_position', json.dumps(old))
        self.assertGreater(penalty('MEDIUM', 35, 'dry'), penalty('MEDIUM', 5, 'dry'))
        self.assertGreater(penalty('MEDIUM', 5, 'rain'), penalty('MEDIUM', 5, 'dry'))

    def test_wet_or_missing_evidence_hands_off(self):
        observed = copy.deepcopy(self.run['observed'])
        observed['issues'] = ['Weather missing'];observed['condition'] = 'unknown'
        result = StrategyWorkflow(observed, self.run['branch'], []).run()
        self.assertTrue(result['needs_human'])

    def test_red_flag_pauses_before_processing_any_lap(self):
        original=self.service.observed
        def red(lap,boundary='start'):
            obs=original(lap,boundary);obs['race_control']='RED';return obs
        self.service.observed=red
        result=self.service.jump(self.owner,self.rid,22,'red',0)
        self.assertEqual(result['branch']['lap'],20)
        self.assertEqual(result['branch']['history'],[])
        self.assertEqual(result['branch']['pause']['key'],'flag-RED')
        self.assertIsNotNone(result['job'])

    def test_export_contains_assumptions_provenance_and_saved_decisions(self):
        self.pit()
        result = self.service.export(self.owner, self.rid)
        self.assertEqual(result['dataset_sha256'], self.service.replay.dataset_digest)
        self.assertIn('assumptions', result)
        self.assertEqual(len(result['run']['memory']), 2)


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.observed = {'lap': 20, 'condition': 'dry', 'race_control': 'GREEN', 'issues': [],
                         'pace_seconds': 105, 'recent_laps': [], 'evidence_refs': [],
                         'observed_compound': 'MEDIUM', 'observed_tyre_age': 4}
        self.branch = new_branch(self.observed, 20)

    def workflow(self, transport):
        return StrategyWorkflow(self.observed, self.branch, [], client=ResponsesClient(transport=transport))

    def test_responses_function_loop_executes_real_tools_then_validates_proposal(self):
        calls = [('recall_memory', {}), ('delegate_rival_analyst', {}), ('delegate_conditions_agent', {}), ('delegate_leclerc_agent', {}), ('compare_strategies', {'stress':'baseline'}),
                 ('propose_decision', {'choice':'hold','rationale':'Hold is robust across sensitivity tests.','needs_human':False})]
        payloads = []
        def transport(payload):
            payloads.append(copy.deepcopy(payload));name,args = calls[len(payloads)-1]
            return {'output':[{'type':'function_call','name':name,'arguments':json.dumps(args),'call_id':'call-'+str(len(payloads))}]}
        result = self.workflow(transport).run('llm')
        self.assertEqual(result['source'], 'openai')
        self.assertEqual(result['status'], 'automatic')
        self.assertFalse(payloads[0]['store'])
        self.assertTrue(any(item['type'] == 'function_call_output' for item in payloads[-1]['input'] if 'type' in item))
        self.assertEqual(len(payloads), 6)

    def test_model_failure_recovers_with_no_autonomous_execution(self):
        def fail(_payload):
            raise ModelError('Injected model outage')
        result = self.workflow(fail).run('llm')
        self.assertEqual(result['source'], 'local_recovery')
        self.assertTrue(result['needs_human'])
        self.assertIn('recover_local', {e['tool'] for e in result['trace']})

    def test_repeated_invalid_tool_calls_exhaust_budget_and_recover(self):
        count = 0
        def malicious(_payload):
            nonlocal count;count += 1
            return {'output':[{'type':'function_call','name':'read_future_results','arguments':'{}','call_id':'x'}]}
        result = self.workflow(malicious).run('llm')
        self.assertEqual(count, 3)
        self.assertEqual(result['source'], 'local_recovery')
        self.assertTrue(result['needs_human'])

    def test_model_cannot_propose_before_evidence_tools(self):
        workflow = StrategyWorkflow(self.observed, self.branch, [])
        with self.assertRaises(ValueError):
            workflow.call('propose_decision', {'choice':'pit_now','rationale':'Trust me','needs_human':False})

    def test_model_cannot_bypass_pit_approval_policy(self):
        workflow = StrategyWorkflow(self.observed, self.branch, [])
        workflow.local()
        workflow.proposal = {'choice':'pit_now','rationale':'Choose a pit','needs_human':False}
        workflow.local = lambda: None
        result = workflow.run('local')
        self.assertTrue(result['needs_human'])

    def test_schema_rejects_extra_arguments_and_invalid_stress(self):
        workflow = StrategyWorkflow(self.observed, self.branch, [])
        with self.assertRaises(ValueError):
            workflow.call('get_race_snapshot', {'through':'future'})
        with self.assertRaises(ValueError):
            workflow.call('compare_strategies', {'stress':'invented'})

    def test_billing_errors_are_specific_redacted_and_not_retried(self):
        client = ResponsesClient(api_key='sk-fake-key-for-testing-only')
        body = json.dumps({'error': {'code':'credit_balance_exhausted', 'message':'Secret must not be echoed: sk-fake-key-for-testing-only'}}).encode()
        error = urllib.error.HTTPError('https://api.openai.com/v1/responses',429,'quota',{},io.BytesIO(body))
        with patch('urllib.request.urlopen',side_effect=error) as request:
            with self.assertRaisesRegex(ModelError,'credit balance is exhausted') as caught:
                client.create({'input':'test'},lambda *args: None)
            self.assertEqual(request.call_count,1)
            self.assertNotIn(client.api_key,str(caught.exception))

    def test_transient_rate_limit_still_retries_once(self):
        client = ResponsesClient(api_key='sk-fake-key-for-testing-only')
        body=json.dumps({'error':{'code':'rate_limit_exceeded'}}).encode()
        error=urllib.error.HTTPError('https://api.openai.com/v1/responses',429,'rate',{},io.BytesIO(body))
        response=MagicMock();response.__enter__.return_value=io.BytesIO(b'{"status":"completed","output":[]}')
        with patch('urllib.request.urlopen',side_effect=[error,response]) as request, patch('time.sleep'):
            self.assertEqual(client.create({'input':'test'},lambda *args: None)['status'],'completed')
            self.assertEqual(request.call_count,2)


if __name__ == '__main__':
    unittest.main()
