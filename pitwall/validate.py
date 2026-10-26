"""Backend pre-deployment matrix: python -m pitwall.validate [--mode llm]."""
import argparse
import json
import tempfile
import time
from datetime import datetime,timezone
from pathlib import Path
from .agents import ResponsesClient,DIRECTOR_INSTRUCTIONS
from .data import ROOT,load_timeline,digest
from .evaluation import CASES,evaluate
from .gemini import GeminiClient
from .service import PitwallService
from .simulation import MODEL


def matrix(*,mode='local',laps=None,cases=None,output=None,client=None,resume=False,interval=10):
    if mode not in ('local','llm'):raise ValueError('Choose local or llm mode')
    laps=laps or ([15] if mode=='llm' else [1,8,15,25,40,56])
    selected=cases or [c['id'] for c in CASES]
    if any(c not in {s['id'] for s in CASES} for c in selected):raise ValueError('Unknown scenario')
    client=client or (GeminiClient() if mode=='llm' else ResponsesClient(api_key=''))
    if mode=='llm' and (client.provider!='gemini' or not client.configured):
        raise ValueError('Configure Gemini for live validation. No paid-provider fallback is permitted.')
    report={'created_at':datetime.now(timezone.utc).isoformat(),'mode':mode,'simulation_version':MODEL['version'],'instructions_sha256':digest(DIRECTOR_INSTRUCTIONS),
            'laps':laps,'cases':selected,'results':[],'not_run':[],
            'scope':'Functional agent, tool and original-proposal checks. Does not certify race physics or optimal strategy.'}
    report['provider']=client.provider if mode=='llm' else 'local'
    report['model']=client.model if mode=='llm' else None
    if resume:
        if not output or not Path(output).exists():raise ValueError('No existing report to resume')
        previous=json.loads(Path(output).read_text())
        if any(previous.get(k)!=report[k] for k in ('mode','provider','model','simulation_version','instructions_sha256','laps','cases')):
            raise ValueError('Resume must use the same mode, model version, laps and cases')
        report['results']=[r for r in previous['results'] if r['verdict']=='pass']
        report['attempt_history']=previous.get('attempt_history',[])+[r for r in previous['results'] if r['verdict']!='pass']
    passed={(r['lap'],r['case']['id']) for r in report['results']}
    def save():
        report['passed']=sum(r['verdict']=='pass' for r in report['results'])
        report['failed']=sum(r['verdict']=='fail' for r in report['results'])
        report['verdict']='pass' if report['failed']==0 and not report['not_run'] else 'fail'
        if output:
            path=Path(output);path.parent.mkdir(parents=True,exist_ok=True)
            temp=path.with_suffix('.tmp');temp.write_text(json.dumps(report,indent=2,allow_nan=False));temp.replace(path)
    with tempfile.TemporaryDirectory() as folder:
        service=PitwallService(Path(folder)/'validation.sqlite',load_timeline(ROOT/'data/austin-2025-demo.json'),client=ResponsesClient(api_key=''))
        try:
            stop_provider=False;live_calls=0
            report['dataset_sha256']=service.replay.dataset_digest;report['rivals_sha256']=service.rivals.digest
            if resume and any(previous.get(k)!=report[k] for k in ('dataset_sha256','rivals_sha256')):
                raise ValueError('Dataset changed; start a fresh validation report')
            for lap in laps:
                snapshot=service.create('validation',lap,'lap-'+str(lap))
                for case in selected:
                    if (lap,case) in passed:continue
                    if stop_provider:
                        report['not_run'].append({'lap':lap,'case':case,'reason':'Live provider unavailable; no further quota requests or paid fallback.'});continue
                    if mode=='llm' and live_calls and interval>0:time.sleep(interval)
                    result=evaluate(snapshot['observed'],snapshot['branch'],snapshot['memory'],case=case,mode=mode,client=client)
                    live_calls+=1
                    report['results'].append({'lap':lap,**result})
                    print('lap '+str(lap)+' / '+case+': '+result['verdict'].upper(),flush=True)
                    # Provider errors must not be disguised as successful local
                    # tests. Stop after a quota/access failure and mark gaps.
                    stop_provider=mode=='llm' and result['decision']['source']=='local_recovery'
                    save()
        finally:service.close()
    save()
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode',choices=['local','llm'],default='local')
    parser.add_argument('--laps',type=int,nargs='+')
    parser.add_argument('--cases',nargs='+',choices=[c['id'] for c in CASES])
    parser.add_argument('--output',type=Path)
    parser.add_argument('--resume',action='store_true',help='Retry failed/unrun cases; retain prior failed attempts and successful results')
    parser.add_argument('--interval',type=float,default=10,help='Seconds between live assessments (default 10); local checks never wait')
    args=parser.parse_args()
    path=args.output or ROOT/'.local/validation'/(args.mode+'-scenarios.json')
    if not 0<=args.interval<=60:parser.error('Interval must be between 0 and 60 seconds')
    try:report=matrix(mode=args.mode,laps=args.laps,cases=args.cases,output=path,resume=args.resume,interval=args.interval)
    except ValueError as error:parser.error(str(error))
    print(str(report['passed'])+' passed; '+str(report['failed'])+' failed; '+str(len(report['not_run']))+' not run. Report: '+str(path),flush=True)
    raise SystemExit(0 if report['verdict']=='pass' else 1)


if __name__=='__main__':main()
