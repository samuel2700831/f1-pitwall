import copy
import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch,MagicMock

from pitwall.agents import ModelError,StrategyWorkflow,TOOLS,DIRECTOR_INSTRUCTIONS,make_client
from pitwall.configure import load_key,save_key
from pitwall.gemini import GeminiClient


def candidate(calls,signature='opaque-signature'):
    parts=[{'functionCall':{'name':name,'args':args,'id':'provider-'+str(i)},
            'thoughtSignature':signature} for i,(name,args) in enumerate(calls)]
    return {'candidates':[{'finishReason':'STOP','content':{'role':'model','parts':parts}}]}


class GeminiTests(unittest.TestCase):
    def setUp(self):
        self.payload={'model':'gemini-3.8-flash','instructions':DIRECTOR_INSTRUCTIONS,
                      'input':[{'role':'user','content':'Assess now'}],'tools':TOOLS,'max_output_tokens':2500}
        self.observed={'lap':20,'condition':'dry','race_control':'GREEN','issues':[],
                       'pace_seconds':105,'recent_laps':[],'evidence_refs':[],
                       'observed_compound':'MEDIUM','observed_tyre_age':4}
        from pitwall.simulation import new_branch
        self.branch=new_branch(self.observed,20)

    @patch.dict(os.environ,{},clear=True)
    def test_gemini_default_never_loads_openai_key(self):
        with patch('pitwall.gemini.load_key',return_value='') as loader:
            self.assertEqual(make_client().provider,'gemini')
            loader.assert_called_once_with(provider='gemini')

    def test_native_schemas_and_forced_tool_selection(self):
        body=GeminiClient(api_key='').request_body(self.payload)
        declarations=body['tools'][0]['functionDeclarations']
        self.assertEqual(body['toolConfig']['functionCallingConfig']['mode'],'ANY')
        self.assertEqual(body['generationConfig']['thinkingConfig']['thinkingLevel'],'LOW')
        self.assertNotIn('parameters',declarations[0])
        self.assertEqual(next(d for d in declarations if d['name']=='compare_strategies')['parameters']['type'],'OBJECT')
        self.assertNotIn('additionalProperties',json.dumps(declarations))

    def test_batched_tool_results_preserve_content_signatures_ids_and_names(self):
        content=candidate([('recall_memory',{}),('delegate_rival_analyst',{}),('delegate_conditions_agent',{}),('delegate_leclerc_agent',{})])
        client=GeminiClient(transport=lambda _body:copy.deepcopy(content))
        output=client.create(self.payload,lambda *args:None)['output']
        self.payload['input'].extend(output)
        for item in output[1:]:
            self.payload['input'].append({'type':'function_call_output','call_id':item['call_id'],'output':'[]'})
        body=client.request_body(self.payload)
        self.assertEqual(body['contents'][1],content['candidates'][0]['content'])
        responses=body['contents'][2]['parts']
        self.assertEqual(len(responses),4)
        self.assertEqual(responses[0]['functionResponse']['name'],'recall_memory')
        self.assertEqual(responses[1]['functionResponse']['id'],'provider-1')
        self.assertEqual(len(body['contents']),3)

    def test_real_workflow_contract_runs_in_two_native_requests(self):
        bodies=[]
        def transport(body):
            bodies.append(copy.deepcopy(body))
            if len(bodies)==1:
                return candidate([('get_race_snapshot',{}),('recall_memory',{}),('delegate_rival_analyst',{}),('delegate_conditions_agent',{}),('delegate_leclerc_agent',{}),
                                  ('compare_strategies',{'stress':'baseline'})])
            return candidate([('propose_decision',{'choice':'hold','rationale':'Hold wins under the checked assumptions.','needs_human':False})])
        workflow=StrategyWorkflow(self.observed,self.branch,[],client=GeminiClient(transport=transport))
        result=workflow.run('llm')
        self.assertEqual(result['source'],'gemini')
        self.assertEqual(result['provider'],'gemini')
        self.assertEqual(result['status'],'automatic')
        self.assertEqual(len(bodies),2)
        self.assertEqual(len(bodies[1]['contents'][-1]['parts']),6)

    def test_quota_recovery_uses_only_local_calculations(self):
        def fail(_body):
            raise ModelError('Gemini quota reached')
        workflow=StrategyWorkflow(self.observed,self.branch,[],client=GeminiClient(transport=fail))
        with patch('pitwall.agents.ResponsesClient.create',side_effect=AssertionError('Paid fallback forbidden')):
            result=workflow.run('llm')
        self.assertEqual(result['source'],'local_recovery')
        self.assertTrue(result['needs_human'])

    def test_http_quota_error_is_not_retried_and_never_echoes_key(self):
        client=GeminiClient(api_key='fake-gemini-key-for-testing-only')
        error=urllib.error.HTTPError('https://generativelanguage.googleapis.com',429,'quota',{},
                io.BytesIO(json.dumps({'error':{'message':client.api_key}}).encode()))
        with patch('urllib.request.urlopen',side_effect=error) as request:
            with self.assertRaisesRegex(ModelError,'quota or rate limit') as caught:
                client.create(self.payload,lambda *args:None)
            self.assertEqual(request.call_count,1)
            self.assertNotIn(client.api_key,str(caught.exception))

    def test_key_is_in_header_and_never_url_or_body(self):
        client=GeminiClient(api_key='fake-gemini-key-for-testing-only')
        response=MagicMock();response.__enter__.return_value=io.BytesIO(json.dumps(candidate([('recall_memory',{})])).encode())
        with patch('urllib.request.urlopen',return_value=response) as request:
            client.create(self.payload,lambda *args:None)
            sent=request.call_args.args[0]
            self.assertNotIn(client.api_key,sent.full_url)
            self.assertNotIn(client.api_key,sent.data.decode())
            self.assertEqual(sent.get_header('X-goog-api-key'),client.api_key)

    def test_rejects_unsafe_model_url(self):
        for model in ('https://other.example','../model','models/gemini-3.8-flash'):
            with self.assertRaises(ValueError):
                GeminiClient(api_key='',model=model)

    def test_rejects_blocked_empty_and_partial_responses(self):
        for result in ({},[],{'candidates':[]},{'candidates':[{'finishReason':'MAX_TOKENS'}]},
                       {'candidates':[{'content':{'role':'model','parts':['bad']}}]}):
            with self.subTest(result=result),self.assertRaises(ModelError):
                GeminiClient(transport=lambda _body:result).create(self.payload,lambda *args:None)

    @patch.dict(os.environ,{},clear=True)
    def test_gemini_private_key_roundtrip_and_exact_paste_repair(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'gemini.json';fake='AIza-fake-gemini-key-for-test-only'
            save_key(fake*2,path,provider='gemini')
            self.assertEqual(load_key(path,provider='gemini'),fake)
            self.assertEqual(path.stat().st_mode&0o777,0o600)
            with self.assertRaises(ValueError):
                save_key('sk-openai-key-must-not-be-sent-to-google',path,provider='gemini')

    @patch.dict(os.environ,{'GEMINI_API_KEY':'gemini-key','GOOGLE_API_KEY':'google-key','OPENAI_API_KEY':'openai-key'},clear=True)
    def test_provider_environment_keys_are_isolated(self):
        self.assertEqual(load_key(provider='gemini'),'gemini-key')
        self.assertEqual(load_key(),'openai-key')


if __name__=='__main__':
    unittest.main()
