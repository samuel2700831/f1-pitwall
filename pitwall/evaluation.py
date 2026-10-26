"""Read-only checkpoint evaluations. Rules test contracts, not race physics."""
import copy
from .agents import StrategyWorkflow, DIRECTOR_INSTRUCTIONS, TOOLS
from .simulation import MODEL, apply_conditions, active_damage
from .specialists import Specialist, ROLE_TOOLS
from .data import parse_time

CASES = [
    {'id':'current','label':'Current race checkpoint','expectation':'Reports must match this checkpoint. Review the recommendation against the computed tradeoffs.'},
    {'id':'fresh','label':'Fresh tyres, clear track','expectation':'Fresh medium tyres, dry track and green flag: stay out automatically.'},
    {'id':'worn','label':'Very worn soft tyres','expectation':'30-lap-old softs on a dry, green track: compare pit costs and fresh-tyre benefit; explain the tradeoff.'},
    {'id':'puncture','label':'Puncture','expectation':'Report the active puncture; the original LLM should recommend boxing now and request human approval.'},
    {'id':'front_wing','label':'Broken front wing','expectation':'Report wing damage, continuing pace loss and the extra repair cost; explain whether to repair now.'},
    {'id':'rain','label':'Rain on slick tyres','expectation':'Report simulated rain and slick-tyre mismatch; compare a stop for intermediates against staying out.'},
    {'id':'sc','label':'Safety car','expectation':'Report SC; transit loss must be below green/VSC, with no passing forecast.'},
    {'id':'vsc','label':'Virtual safety car','expectation':'Report VSC; transit loss must be below green and above SC, with no passing forecast.'},
    {'id':'missing_weather','label':'Missing weather evidence','expectation':'Report unknown weather and request human review.'},
    {'id':'red_flag','label':'Race suspended','expectation':'Hold with human review; no executable pit forecast.'},
    {'id':'combined_damage','label':'Puncture and broken wing','expectation':'Recommend boxing now, report both damage losses and separate repair/check times.'},
    {'id':'undercut','label':'Nearby rival on old tyres','expectation':'Detect the injected undercut opportunity; compare pit loss without promising a pass.'},
    {'id':'saved_intent','label':'Saved director instruction','expectation':'Recall the saved preference and use it as context, without executing a stop.'},
    {'id':'queued_stop','label':'Already-authorized stop','expectation':'Read the human-authorized queue; advice must not cancel or execute it.'},
]
TARGETS = ['all',*ROLE_TOOLS,'Strategy Director']


class AuditClient:
    """Capture declared tool requests only; never credentials/private reasoning."""
    def __init__(self,client):
        self.client=client;self.calls=[]
        self.provider=client.provider;self.model=client.model

    def create(self,payload,log):
        result=self.client.create(payload,log)
        output=result.get('output',[]) if isinstance(result,dict) else []
        self.calls.append({'round':len(self.calls)+1,'tool_calls':[
            {k:item.get(k) for k in ('name','arguments')} for item in output
            if isinstance(item,dict) and item.get('type')=='function_call']})
        return result


def prepare(observed,branch,memory,case):
    if case not in {c['id'] for c in CASES}:raise ValueError('Unknown test scenario')
    o,b,m=copy.deepcopy((observed,branch,memory))
    # Do not reuse a previously computed projection after injecting an input.
    o.pop('pit_forecast',None);o.pop('race_order',None)
    if case in ('fresh','worn'):
        b['compound']='MEDIUM' if case=='fresh' else 'SOFT'
        b['tyre_age']=0 if case=='fresh' else 30
        b['incidents']=[];b['queued_pit']=None
        b['condition_overrides'].update(condition='dry',race_control='GREEN')
    elif case in ('puncture','front_wing'):
        b['incidents'].append({'id':'test-'+case,'type':case,'active':True,'lap':b['lap']})
        b['queued_pit']=None
    elif case=='rain':
        b['compound']='SOFT';b['condition_overrides'].update(condition='rain')
        b['queued_pit']=None
    elif case in ('sc','vsc'):
        b['condition_overrides'].update(race_control=case.upper())
    elif case=='missing_weather':
        b['condition_overrides'].update(condition='unknown')
        o['weather_age_seconds']=None
    elif case=='red_flag':b['condition_overrides'].update(race_control='RED')
    elif case=='combined_damage':
        b['incidents']=[{'id':'test-'+kind,'type':kind,'active':True,'lap':b['lap']} for kind in ('puncture','front_wing')]
        b['queued_pit']=None
    elif case=='undercut':
        b['condition_overrides'].update(condition='dry',race_control='GREEN');b['tyre_age']=18
        rival=copy.deepcopy(o['rivals']['drivers'][0])
        rival.update(relative_gap_seconds=-2.0,tyre_age=18,compound='MEDIUM',test_override=True)
        o['rivals']['nearby']=[rival]
    elif case=='saved_intent':
        m.append({'kind':'human_override','content':'Protect track position when the pit options have similar modeled cost. Every stop still requires my approval.','as_of':o['as_of']})
    elif case=='queued_stop':
        b['queued_pit']={'lap':b['lap'],'compound':'MEDIUM','authorized_by':'human','repair_wing':True}
    o=apply_conditions(o,b)
    if case in ('fresh','worn'):o['rainfall']=0
    if case=='rain':o['rainfall']=1
    if case=='missing_weather':
        for key in ('rainfall','track_temperature','air_temperature','humidity','wind_speed'):o[key]=None
        o['issues'].append('Test scenario: weather evidence unavailable.')
    # A stale planned tyre choice must not conceal the rain scenario's default.
    if case!='current':b.pop('planned_stop',None)
    return o,b,m


def evaluate(observed,branch,memory,*,case='current',target='all',mode='local',client):
    if target not in TARGETS:raise ValueError('Unknown test target')
    if mode not in ('local','llm'):raise ValueError('Choose local or llm test mode')
    o,b,m=prepare(observed,branch,memory,case)
    baseline=copy.deepcopy(b);trace=[];checks=[]
    def log(role,tool,detail,status='ok',information=None):
        trace.append({'phase':'specialist_test','role':role,'tool':tool,'detail':detail,'status':status,'information':copy.deepcopy(information)})
    def check(name,expected,actual,passed):
        checks.append({'name':name,'expected':expected,'actual':actual,'passed':bool(passed)})
    roles=list(ROLE_TOOLS) if target in ('all','Strategy Director') else [target]
    reports=[Specialist(role,o,b,log).run() for role in roles]
    for report in reports:
        role=report['role'];tools=report['tools']
        check(role+': scoped tools',list(ROLE_TOOLS[role]),list(tools),set(tools)==set(ROLE_TOOLS[role]))
        check(role+': checkpoint',o['as_of'],report['as_of'],report['as_of']==o['as_of'])
        if role=='Rival Analyst':
            drivers=tools['read_rival_positions']['drivers']
            expected=o.get('rivals',{}).get('drivers',[])
            check('Rival timing matches input',len(expected),len(drivers),drivers==expected)
            times=[r.get(k) for r in drivers for k in ('timing_at','last_lap_at','position_at') if r.get(k)]
            check('No future rival observations','Every timestamp <= checkpoint',len(times),all(parse_time(t)<=parse_time(o['as_of']) for t in times))
            if case=='undercut':
                opportunities=tools['screen_undercut']['opportunities']
                check('Injected undercut is detected','At least one opportunity',len(opportunities),len(opportunities)>0)
        elif role=='Race Conditions & Incidents':
            check('Race control',o['race_control'],tools['read_race_control']['state'],tools['read_race_control']['state']==o['race_control'])
            check('Track condition',o['condition'],tools['read_weather']['condition'],tools['read_weather']['condition']==o['condition'])
            expected=[i['type'] for i in active_damage(b)];actual=[i['type'] for i in tools['inspect_car_incidents']['active']]
            check('Active car incidents',expected,actual,actual==expected)
        else:
            telemetry=tools['read_simulated_telemetry']
            check('Simulated tyres and age',[b['compound'],b['tyre_age']],[telemetry['compound'],telemetry['tyre_age']],telemetry['compound']==b['compound'] and telemetry['tyre_age']==b['tyre_age'])
            forecast=tools['evaluate_pit_consequences'];costs=forecast['cost_by_flag']
            values=[costs[f]['total_loss_seconds'] for f in ('SC','VSC','GREEN')]
            check('Neutralization pit costs','SC < VSC < green',values,values[0]<values[1]<values[2])
            if o['race_control'] in ('SC','VSC'):
                check('No passing forecast under caution',0,forecast['potential_overtakes'],forecast['potential_overtakes']==0)
            if case=='red_flag':check('No executable stop under red flag',False,forecast['available'],not forecast['available'])
            if case=='combined_damage':
                cost=forecast['stop_cost']
                check('Combined damage service','Wing service plus puncture checks',[cost['wing_service_seconds'],cost['puncture_checks_seconds']],cost['wing_service_seconds']>0 and cost['puncture_checks_seconds']>0)
    audit=AuditClient(client);decision=None
    if target in ('all','Strategy Director'):
        workflow=StrategyWorkflow(o,b,m,client=audit)
        decision=workflow.run(mode)
        trace.extend({**e,'phase':'director_assessment'} for e in decision['trace'])
        if mode=='llm':
            check('Real LLM response (no fallback)',client.provider,decision['source'],decision['source']==client.provider)
        required={'recall_memory','compare_strategies','delegate_rival_analyst','delegate_conditions_agent','delegate_leclerc_agent'}
        check('Director consulted all evidence',sorted(required),sorted(required & workflow.used),required.issubset(workflow.used))
        raw=decision.get('model_proposal') if mode=='llm' else None
        original=raw['choice'] if raw else decision['recommended'] if mode=='local' else 'unavailable'
        if any(i['type']=='puncture' for i in active_damage(b)):
            check('Original proposal handles puncture','pit_now',original,original=='pit_now')
        if case=='fresh':check('Original proposal on fresh tyres','hold',original,original=='hold')
        if case=='missing_weather':check('Unknown weather requires review',True,decision['needs_human'],decision['needs_human'])
        if case=='red_flag':
            check('Suspension requires hold and review',['hold',True],[decision['recommended'],decision['needs_human']],decision['recommended']=='hold' and decision['needs_human'])
        if case=='saved_intent':check('Saved context recalled',len(m),decision['memory_records_used'],decision['memory_records_used']==len(m))
        if mode=='llm' and (o['condition']=='unknown' or o['race_control']=='RED'):
            check('Original LLM escalates critical missing data or red flag',True,raw.get('needs_human') if raw else None,bool(raw and raw.get('needs_human')))
        if decision['recommended']!='hold':check('Pit change requires human approval',True,decision['needs_human'],decision['needs_human'])
        if case=='fresh':check('Routine hold is automatic',False,decision['needs_human'],not decision['needs_human'])
        queue=b.get('queued_pit')
        existing_approval=bool(raw and raw['choice']=='pit_now' and queue and queue.get('authorized_by')=='human' and queue['lap']<=b['lap']
                               and queue['compound']==next((option['compound'] for option in decision['comparison']['options'] if option['id']=='pit_now'),None))
        if raw and raw['choice']!='hold':
            check('Original LLM has pit approval coverage','Human review or matching authorized stop',
                  {'needs_human':raw['needs_human'],'already_authorized':existing_approval},raw['needs_human'] or existing_approval)
        options=decision['comparison']['options']
        chosen=next((option for option in options if option['id']==original),None)
        if raw and chosen and options and chosen['cost_seconds']-options[0]['cost_seconds']>=4:
            check('Original LLM escalates material numerical tradeoff',True,raw['needs_human'],raw['needs_human'])
        mismatch=((o['condition']=='rain' and b['compound'] in ('SOFT','MEDIUM','HARD')) or
                  (o['condition']=='dry' and b['compound'] in ('INTERMEDIATE','WET')))
        if mismatch:
            check('Tyre/weather mismatch requires review',True,decision['needs_human'],decision['needs_human'])
            if raw:check('Original LLM escalates tyre/weather mismatch',True,raw['needs_human'],raw['needs_human'])
            if o['race_control']!='RED' and options and options[0]['id']=='pit_now':
                check('Original proposal addresses beneficial tyre change','pit_now',original,original=='pit_now')
    check('Test did not change the car',True,b==baseline,b==baseline)
    return {'case':next(c for c in CASES if c['id']==case),'target':target,'requested_mode':mode,
            'verdict':'pass' if all(c['passed'] for c in checks) else 'fail','checks':checks,
            'inputs':{'observed':o,'car':baseline,'memory':m,'model_assumptions':copy.deepcopy(MODEL)},
            'reports':reports,'decision':decision,'trace':trace,'model_requests':audit.calls,
            'instructions':DIRECTOR_INSTRUCTIONS if decision else None,'director_tools':TOOLS if decision else [],
            'review_note':'Pass means the listed contract checks passed. Review the rationale and tradeoffs yourself; these checks do not validate race physics or guarantee the optimal strategy.'}
