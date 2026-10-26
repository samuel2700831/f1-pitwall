"""Historical opponents, indexed by availability time. No future data in reports."""
import bisect
import json
import statistics
from datetime import timedelta
from pathlib import Path
from .data import ROOT, parse_time, iso_time, digest


def build(raw, session_key=9939, controlled_driver=16):
    for rows in raw.values():
        if any(r.get('session_key') != session_key for r in rows):
            raise ValueError('Rival import contains a different session')
    events = []
    starts = {(r['driver_number'], r['lap_number']): r.get('date_start') for r in raw['laps']}
    def add(kind, at, row):
        if at and row.get('driver_number') != controlled_driver:
            events.append({'kind': kind, 'available_at': iso_time(parse_time(at)), 'payload': row})
    for endpoint in ('position', 'intervals'):
        for row in raw[endpoint]:
            add(endpoint, row.get('date'), row)
    for row in raw['laps']:
        if row.get('date_start') and row.get('lap_duration'):
            at = parse_time(row['date_start']) + timedelta(seconds=row['lap_duration'])
            add('completed_lap', iso_time(at), row)
    for row in raw['stints']:
        p = {k:v for k,v in row.items() if k != 'lap_end'}
        add('tyres', starts.get((row['driver_number'], row['lap_start'])), p)
    for row in raw['pit']:
        add('pit_entry', row.get('date'), row)
        duration = row.get('lane_duration') or row.get('pit_duration')
        if row.get('date') and duration:
            add('pit_exit', iso_time(parse_time(row['date']) + timedelta(seconds=duration)), row)
    events.sort(key=lambda e: e['available_at'])
    return {'schema_version':1, 'session_key':session_key, 'source':'https://openf1.org/docs/',
            'drivers':[{k:r.get(k) for k in ('driver_number','full_name','name_acronym','team_name')} for r in raw['drivers'] if r['driver_number'] != controlled_driver], 'events':events}


class RivalFeed:
    def __init__(self, data=None):
        self.data = data or {'drivers':[], 'events':[]}
        self.digest = digest(self.data)
        self.times = [e['available_at'] for e in self.data['events']]
        if self.times != sorted(self.times):
            raise ValueError('Rival feed must be chronological')

    def snapshot(self, as_of, position=None, leclerc_gap=None):
        end = bisect.bisect_right(self.times, as_of)
        latest, laps, pits = {}, {}, []
        cutoff = parse_time(as_of)
        for e in self.data['events'][:end]:
            p = e['payload']; number = p['driver_number']
            latest.setdefault(number, {})[e['kind']] = e
            if e['kind'] == 'completed_lap':
                laps.setdefault(number, []).append(p)
            if e['kind'] in ('pit_entry','pit_exit') and (cutoff-parse_time(e['available_at'])).total_seconds() <= 180:
                pits.append({'driver_number':number, 'event':e['kind'], 'at':e['available_at'], 'lap':p.get('lap_number')})
        drivers = []
        names = {d['driver_number']:d for d in self.data['drivers']}
        for number, values in latest.items():
            pos = values.get('position',{}).get('payload',{}).get('position')
            recent = [r for r in laps.get(number, [])[-8:] if not r.get('is_pit_out_lap')]
            representative=[r['lap_duration'] for r in recent if r.get('lap_number',0)>1 and isinstance(r.get('lap_duration'),(int,float)) and 75<r['lap_duration']<115][-3:]
            stint=values.get('tyres',{}).get('payload',{})
            completed=laps.get(number,[])
            lap=completed[-1]['lap_number']+1 if completed else None
            age=max(0,lap-stint['lap_start']+(stint.get('tyre_age_at_start') or 0)) if lap and stint.get('lap_start') else None
            gap=values.get('intervals',{}).get('payload',{}).get('gap_to_leader')
            relative=round(gap-leclerc_gap,3) if isinstance(gap,(int,float)) and isinstance(leclerc_gap,(int,float)) else None
            drivers.append({**names.get(number, {'driver_number':number}), 'position':pos,
                'tyre_age':age,'gap_to_leader_seconds':gap,'relative_gap_seconds':relative,
                'interval':values.get('intervals',{}).get('payload',{}).get('interval'),
                'compound':values.get('tyres',{}).get('payload',{}).get('compound','UNKNOWN'),
                'last_lap_seconds':completed[-1].get('lap_duration') if completed else None,
                'pace_seconds':round(statistics.median(representative),3) if representative else None,
                'representative_laps':len(representative),'pace_method':'Median of up to three completed 75–115 s laps excluding lap one and pit-out laps; illustrative Austin clean-lap filter.',
                'timing_at':values.get('intervals',{}).get('available_at'),
                'last_lap_at':values.get('completed_lap',{}).get('available_at'),
                'pit_status':'In pit lane' if values.get('pit_entry',{}).get('available_at','')>values.get('pit_exit',{}).get('available_at','') else 'On track',
                'position_at':values.get('position',{}).get('available_at')})
        drivers.sort(key=lambda d:d['position'] or 99)
        nearby = [d for d in drivers if position and d['position'] and abs(d['position']-position)<=3]
        for p in pits:
            p['name'] = names.get(p['driver_number'],{}).get('full_name',str(p['driver_number']))
        return {'as_of':as_of, 'available':bool(drivers), 'drivers':drivers, 'nearby':nearby, 'recent_pits':pits,
                'reference':'Rivals follow historical observations. Gaps and positions are historical references, not simulated classification.'}


def main():
    from .fetch_data import fetch
    raw, sources = {}, {}
    folder = ROOT/'data'/'raw'; folder.mkdir(exist_ok=True,parents=True)
    for endpoint in ('drivers','laps','stints','position','intervals','pit'):
        path = folder/('rivals-'+endpoint+'.json')
        rows = json.loads(path.read_text()) if path.exists() else fetch(endpoint, {'session_key':9939})
        path.write_text(json.dumps(rows, separators=(',',':')))
        raw[endpoint] = rows
        sources[endpoint] = {'query':{'session_key':9939}, 'count':len(rows), 'sha256':digest(rows)}
        print(endpoint+': '+str(len(rows))+' records',flush=True)
    data = build(raw)
    target = ROOT/'data'/'spa-2025-rivals.json'
    temp = target.with_suffix('.tmp');temp.write_text(json.dumps(data,separators=(',',':'),allow_nan=False));temp.replace(target)
    (ROOT/'data'/'spa-2025-rivals-source.json').write_text(json.dumps({'provider':'https://openf1.org/docs/','sources':sources,'sha256':digest(data)},indent=2))
    print('Saved '+str(len(data['events']))+' time-gated rival events',flush=True)

if __name__ == '__main__':
    main()
