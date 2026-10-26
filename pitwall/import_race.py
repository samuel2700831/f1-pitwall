"""Import the Austin race and remove Leclerc's historical strategy from web evidence."""
import copy
import json
from datetime import datetime,timezone
from .data import ROOT,ENDPOINTS,build_timeline,digest
from .fetch_data import fetch
from .rivals import build as build_rivals
from .simulation import penalty

AUSTIN={'year':2025,'country_name':'United States','location':'Austin','circuit':'Circuit of the Americas',
        'session_name':'Race','session_key':9888,'meeting_key':1271,'laps':56,'slug':'austin-2025'}


def normalize_controlled(timeline, raw):
    """Keep lap clock and clean pace; no later tyre/stop strategy in agent evidence.

    Wear-neutral pace is an illustrative estimate using only each completed lap's
    historical tyre state, not a fitted physical model. Slow transition laps are
    excluded from pace. Their original timestamps remain the replay clock only.
    """
    result=copy.deepcopy(timeline)
    first=min(raw['stints'],key=lambda r:r['lap_start'])
    pit_laps={p.get('lap_number') for p in raw['pit']}
    events=[]
    for e in result['events']:
        p=e['payload']
        if e['kind']=='pit_visit_completed':continue
        if e['kind']=='tyre_stint_started':
            if p['lap_start']!=first['lap_start']:continue
        if e['kind']=='race_control' and p.get('driver_number')==16 and 'PIT' in (p.get('message') or '').upper():continue
        if e['kind']=='completed_lap':
            number=p['lap_number'];seconds=p.get('lap_duration')
            usable=seconds and 75<seconds<150 and not p.get('is_pit_out_lap') and number not in pit_laps and number>1
            stints=[r for r in raw['stints'] if r['lap_start']<=number]
            stint=max(stints,key=lambda r:r['lap_start'])
            age=number-stint['lap_start']+(stint.get('tyre_age_at_start') or 0)
            baseline=round(seconds-penalty(stint['compound'],age,'dry'),3) if usable else None
            # Only the clean, wear-neutral pace estimate reaches the web tools.
            e['payload']={'driver_number':16,'lap_number':number,'date_start':p['date_start'],
                          'lap_duration':baseline,'pace_reference':'estimated wear-neutral baseline' if baseline else 'unavailable'}
        events.append(e)
    result['events']=events
    result['controlled_strategy_removed']=True
    return result


def main():
    project=json.loads((ROOT/'project.json').read_text())
    project['race']=AUSTIN;project['data_bundle']='austin-2025-demo.json'
    folder=ROOT/'data'/'raw'/'austin-2025';folder.mkdir(parents=True,exist_ok=True)
    raw,sources={},{}
    for endpoint in ('sessions','drivers',*ENDPOINTS):
        path=folder/(endpoint+'.json')
        rows=json.loads(path.read_text()) if path.exists() else fetch(endpoint,{'session_key':9888})
        if any(r.get('session_key')!=9888 for r in rows):raise ValueError('Wrong race in import')
        path.write_text(json.dumps(rows,separators=(',',':'),allow_nan=False))
        raw[endpoint]=rows;sources[endpoint]={'query':{'session_key':9888},'count':len(rows),'sha256':digest(rows)}
        print(endpoint+': '+str(len(rows))+' records',flush=True)
    controlled={k:([r for r in rows if r.get('driver_number')==16] if k not in ('sessions','weather','race_control') else rows) for k,rows in raw.items()}
    timeline=normalize_controlled(build_timeline(controlled,project),controlled)
    rivals=build_rivals(raw,9888,16)
    for name,data in [('austin-2025-demo.json',timeline),('austin-2025-rivals.json',rivals)]:
        path=ROOT/'data'/name;temp=path.with_suffix('.tmp');temp.write_text(json.dumps(data,separators=(',',':'),allow_nan=False));temp.replace(path)
    manifest={'imported_at':datetime.now(timezone.utc).isoformat(),'provider':'https://openf1.org/docs/','sources':sources,
              'timeline_sha256':digest(timeline),'rivals_sha256':digest(rivals),'controlled_strategy_removed':True,
              'pace':'Completed clean laps only, estimated historical tyre wear subtracted; pit-affected laps excluded. Historical boundary timestamps used as replay clock.'}
    (ROOT/'data'/'austin-2025-source.json').write_text(json.dumps(manifest,indent=2)+'\n')
    (ROOT/'project.json').write_text(json.dumps(project,indent=2)+'\n')
    print('Imported Austin 2025: '+str(len(timeline['events']))+' controlled/world events; '+str(len(rivals['events']))+' rival events',flush=True)

if __name__=='__main__':main()
