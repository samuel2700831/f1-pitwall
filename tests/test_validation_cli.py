import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from pitwall.agents import ResponsesClient,ModelError
from pitwall.validate import matrix


class StubGemini:
    provider='gemini';model='test-model';configured=True
    def create(self,payload,log):
        calls=[('recall_memory',{}),('delegate_rival_analyst',{}),('delegate_conditions_agent',{}),('delegate_leclerc_agent',{}),('compare_strategies',{'stress':'baseline'})] if len(payload['input'])==1 else [('propose_decision',{'choice':'hold','rationale':'Hold after reading the evidence.','needs_human':False})]
        return {'output':[{'type':'function_call','call_id':str(i),'name':name,'arguments':json.dumps(args)} for i,(name,args) in enumerate(calls)]}


class ValidationCliTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'report.json'
        self.addCleanup(self.temp.cleanup)

    def run_matrix(self,**kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return matrix(laps=[15],cases=['current','fresh'],output=self.path,interval=0,**kwargs)

    def test_backend_report_is_repeatable_and_contains_data_prompt_fingerprints(self):
        report=self.run_matrix()
        self.assertEqual(report['passed'],2);self.assertEqual(report['failed'],0)
        self.assertEqual(len(report['dataset_sha256']),64);self.assertEqual(len(report['instructions_sha256']),64)
        resumed=self.run_matrix(resume=True)
        self.assertEqual(resumed['passed'],2);self.assertEqual(len(resumed['results']),2)

    def test_provider_failure_stops_matrix_and_resume_preserves_failure_history(self):
        class Failed(StubGemini):
            def create(self,payload,log):raise ModelError('Rate limit test')
        failed=self.run_matrix(mode='llm',client=Failed())
        self.assertEqual(failed['failed'],1);self.assertEqual(len(failed['not_run']),1)
        recovered=self.run_matrix(mode='llm',client=StubGemini(),resume=True)
        self.assertEqual(recovered['verdict'],'pass');self.assertEqual(recovered['passed'],2)
        self.assertEqual(len(recovered['attempt_history']),1)

    def test_resume_rejects_changed_prompt_model_or_dataset(self):
        report=self.run_matrix()
        for field in ('instructions_sha256','model','dataset_sha256'):
            changed={**report,field:'changed'};self.path.write_text(json.dumps(changed))
            with self.subTest(field=field),self.assertRaises(ValueError):self.run_matrix(resume=True)

    def test_live_validation_never_uses_a_paid_provider(self):
        with self.assertRaisesRegex(ValueError,'No paid-provider'):self.run_matrix(mode='llm',client=ResponsesClient(api_key=''))


if __name__=='__main__':unittest.main()
