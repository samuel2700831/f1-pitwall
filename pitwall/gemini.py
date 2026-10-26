"""Gemini generateContent adapter for the existing bounded strategy tool loop.

Native model content, including opaque thought signatures, is echoed unchanged
between requests. Only tool requests are normalized for the workflow; neither
provider content nor credentials are included in user-visible tool traces.
"""
from __future__ import annotations

import copy
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.request

from .agents import ModelError
from .configure import load_key

DEFAULT_MODEL = 'gemini-3.5-flash-lite'


def native_schema(schema):
    """Use Gemini's documented Schema subset, with application validation too."""
    if not isinstance(schema, dict):
        return schema
    result = {}
    for key, value in schema.items():
        if key == 'additionalProperties':
            continue
        if key == 'type':
            result[key] = value.upper()
        elif key == 'properties':
            result[key] = {name: native_schema(item) for name, item in value.items()}
        elif key == 'items':
            result[key] = native_schema(value)
        else:
            result[key] = copy.deepcopy(value)
    return result


class GeminiClient:
    provider = 'gemini'
    provider_name = 'Gemini'

    def __init__(self, api_key=None, model=None, transport=None):
        self.api_key = api_key if api_key is not None else load_key(provider='gemini')
        self.model = model or os.environ.get('GEMINI_MODEL', DEFAULT_MODEL)
        if not re.fullmatch(r'[a-zA-Z0-9_.-]+', self.model):
            raise ValueError('Use a Gemini model ID, without a URL or path')
        self.transport = transport

    @property
    def configured(self):
        return bool(self.api_key or self.transport)

    def request_body(self, payload):
        contents, mapping = [], {}
        # Conversation state remains in this workflow. No cloud conversation ID,
        # file store, search grounding, cache, or paid-provider fallback is used.
        for item in payload['input']:
            kind = item.get('type')
            if kind == 'gemini_content':
                contents.append(copy.deepcopy(item['content']))
                mapping.update(item['calls'])
            elif kind == 'function_call':
                continue  # Already preserved in the complete native model turn.
            elif kind == 'function_call_output':
                call = mapping.get(item['call_id'])
                if call is None:
                    raise ModelError('Gemini tool result has no matching request.')
                result = {'name': call['name'], 'response': {'result': json.loads(item['output'])}}
                if call.get('id'):
                    result['id'] = call['id']
                part = {'functionResponse': result}
                if contents and contents[-1]['role'] == 'user':
                    contents[-1]['parts'].append(part)
                else:
                    contents.append({'role':'user', 'parts':[part]})
            elif item.get('role') in ('user', 'assistant'):
                contents.append({'role':'user' if item['role']=='user' else 'model',
                                 'parts':[{'text': item['content']}]})
            else:
                raise ModelError('Unsupported conversation item for Gemini.')
        declarations = []
        for tool in payload['tools']:
            declaration = {'name':tool['name'], 'description':tool['description']}
            if tool['parameters'].get('properties'):
                declaration['parameters'] = native_schema(tool['parameters'])
            declarations.append(declaration)
        generation = {'maxOutputTokens':payload.get('max_output_tokens',2500)}
        if self.model.startswith(('gemini-3','gemini-4')):
            generation['thinkingConfig'] = {'thinkingLevel':'LOW'}
        return {'systemInstruction': {'parts':[{'text':payload['instructions'] +
                    '\nBatch independent evidence tool calls in one response when possible to conserve quota. '
                    'Wait for their results before proposing a decision.'}]},
                'contents':contents, 'tools':[{'functionDeclarations':declarations}],
                'toolConfig':{'functionCallingConfig':{'mode':'ANY'}},
                'generationConfig':generation}

    def request(self, body, log):
        if self.transport:
            return self.transport(body)
        if not self.api_key:
            raise ModelError('Gemini is not configured. Open Configure Gemini.command or set GEMINI_API_KEY on the server.')
        context = ssl.create_default_context()
        try:
            import certifi
            context = ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            pass
        for attempt in range(2):
            request = urllib.request.Request(
                'https://generativelanguage.googleapis.com/v1beta/models/' + self.model + ':generateContent',
                data=json.dumps(body,allow_nan=False).encode(),
                headers={'x-goog-api-key':self.api_key,'Content-Type':'application/json'},method='POST')
            try:
                with urllib.request.urlopen(request,context=context,timeout=25) as response:
                    return json.load(response)
            except urllib.error.HTTPError as error:
                # Never echo raw provider bodies: they can contain keys or input.
                if error.code == 429:
                    raise ModelError('Gemini quota or rate limit reached. Wait for it to reset or use Local analysis. No paid provider was called.') from None
                if error.code in (401,403):
                    raise ModelError('Gemini rejected the key or project access. Check the AI Studio key and project, then retry.') from None
                if error.code == 404:
                    raise ModelError('The configured Gemini model is unavailable to this project. Check GEMINI_MODEL and AI Studio model access.') from None
                if error.code in (500,502,503,504) and attempt==0:
                    log('Strategy Director','model_retry','Gemini is temporarily unavailable; retrying once.','recovery')
                    time.sleep(.4)
                    continue
                if error.code in (500,502,503,504):
                    raise ModelError('Gemini is temporarily unavailable after one retry. Use Local analysis or try again later.') from None
                raise ModelError('Gemini request failed (HTTP ' + str(error.code) + '). Check model/key settings or use Local analysis.') from None
            except (urllib.error.URLError,TimeoutError,json.JSONDecodeError):
                if attempt==0:
                    log('Strategy Director','model_retry','Gemini connection failed; retrying once.','recovery')
                    continue
                raise ModelError('Gemini connection failed after one retry. Local analysis remains available.') from None
        raise ModelError('Gemini retry budget exhausted.')

    def create(self, payload, log):
        result = self.request(self.request_body(payload),log)
        if not isinstance(result,dict):
            raise ModelError('Invalid Gemini response; no strategy was executed.')
        candidates = result.get('candidates')
        if not isinstance(candidates,list) or not candidates or not isinstance(candidates[0],dict):
            raise ModelError('Gemini did not return an actionable candidate. No strategy was executed.')
        candidate = candidates[0]
        if candidate.get('finishReason') not in (None,'STOP'):
            raise ModelError('Gemini response was blocked or incomplete. No strategy was executed.')
        content = candidate.get('content')
        if not isinstance(content,dict) or content.get('role')!='model' or not isinstance(content.get('parts'),list):
            raise ModelError('Gemini returned invalid model content.')
        calls, output = {}, []
        turn = sum(item.get('type')=='gemini_content' for item in payload['input'])
        for index,part in enumerate(content['parts']):
            if not isinstance(part,dict):
                raise ModelError('Gemini returned an invalid content part.')
            call = part.get('functionCall')
            if call is not None:
                if not isinstance(call,dict) or not isinstance(call.get('name'),str):
                    raise ModelError('Gemini returned an invalid tool call.')
                call_id = 'gemini-' + str(turn) + '-' + str(index)
                calls[call_id] = {'name':call['name'], 'id':call.get('id')}
                output.append({'type':'function_call','name':call['name'],
                               'arguments':json.dumps(call.get('args',{}),allow_nan=False),'call_id':call_id})
        return {'output':[{'type':'gemini_content','content':copy.deepcopy(content),'calls':calls},*output]}
