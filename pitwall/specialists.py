"""Three bounded analytical specialists. Only the Strategy Director uses an LLM.

Each specialist runs its own read-only tools against the current checkpoint.
They cannot advance time, read the archive, execute a stop, or edit memory.
"""
import copy
from datetime import datetime,timezone
from .race_model import pit_forecast,classification,stop_cost
from .simulation import MODEL, active_damage, damage_penalty, penalty, undercut_options

ROLE_TOOLS = {
    'Rival Analyst': ('read_rival_positions', 'read_rival_pits', 'read_rival_pace', 'screen_undercut'),
    'Race Conditions & Incidents': ('read_weather', 'read_race_control', 'inspect_car_incidents'),
    'Leclerc Performance': ('read_simulated_telemetry', 'estimate_tyre_degradation', 'read_stint_history', 'evaluate_pit_consequences'),
}


class Specialist:
    def __init__(self, role, observed, branch, log):
        if role not in ROLE_TOOLS:
            raise ValueError('Unknown specialist')
        self.role, self.observed, self.branch, self.log = role, observed, branch, log

    def tool(self, name):
        if name not in ROLE_TOOLS[self.role]:
            raise ValueError('Tool is outside this specialist scope')
        o,b = self.observed,self.branch
        rivals = o.get('rivals',{})
        if name == 'read_rival_positions':
            value = {'drivers':rivals.get('drivers',[]),'nearby':rivals.get('nearby',[]), 'reference':rivals.get('reference','Rival feed unavailable.'), 'available':rivals.get('available',False)}
        elif name == 'read_rival_pits':
            value = rivals.get('recent_pits',[])
        elif name == 'read_rival_pace':
            value = [{'name':r.get('full_name'), 'last_completed_lap_seconds':r.get('last_lap_seconds'), 'compound':r.get('compound')} for r in rivals.get('nearby',[])]
        elif name == 'screen_undercut':
            value={'opportunities':undercut_options(b,o),'assumption':'Historical gaps are references; potential tyre advantage does not guarantee an overtake.'}
        elif name == 'evaluate_pit_consequences':
            planned=b.get('planned_stop',{})
            value=pit_forecast(b,o,planned.get('compound'),planned.get('repair_wing',True))
        elif name == 'read_weather':
            value = {k:o.get(k) for k in ('condition','rainfall','track_temperature','air_temperature','humidity','wind_speed','weather_age_seconds','simulated_overrides')}
        elif name == 'read_race_control':
            value = {'state':o['race_control'], 'messages':o.get('recent_messages',[])}
        elif name == 'inspect_car_incidents':
            value = {'active':active_damage(b), 'modeled_loss_per_lap_seconds':damage_penalty(b)}
        elif name == 'read_simulated_telemetry':
            value = {'simulated_position':classification(b,o)['position'],'race_gap_seconds':b.get('race_gap_seconds'), 'compound':b['compound'], 'tyre_age':b['tyre_age'], 'pit_count':b['pit_count'],
                     'last_simulated_laps':b['history'][-3:], 'historical_pace_reference':o.get('pace_seconds'),
                     'queued_human_pit':b['queued_pit'], 'note':'Simulated lap pace uses historical pace as a baseline plus illustrative tyre/damage/pit effects.'}
        elif name == 'estimate_tyre_degradation':
            value = {'penalty_seconds':round(penalty(b['compound'],b['tyre_age'],o['condition']),2),
                     'assumed_degradation_seconds_per_lap':MODEL['degradation'].get(b['compound']),
                     'cliff_age_laps':MODEL['cliff_age'].get(b['compound']),
                     'laps_until_cliff':max(0,MODEL['cliff_age'].get(b['compound'],0)-b['tyre_age']), 'damage_seconds':damage_penalty(b), 'condition':'Past modeled cliff' if b['tyre_age']>=MODEL['cliff_age'].get(b['compound'],999) else 'Approaching drop-off' if b['tyre_age']>=MODEL['cliff_age'].get(b['compound'],999)-2 else 'Within modeled operating window', 'calibrated':False}
        else:
            value = {'entry_lap':b['entry_lap'], 'stops':[h for h in b['history'] if h['pit']], 'current_age':b['tyre_age']}
        self.log(self.role,name,'Read '+('the 19-driver timing feed' if name=='read_rival_positions' else name.replace('_',' '))+' at replay '+str(o.get('as_of'))+'.','ok',copy.deepcopy(value))
        return copy.deepcopy(value)

    def run(self):
        data = {name:self.tool(name) for name in ROLE_TOOLS[self.role]}
        o,b = self.observed,self.branch
        if self.role == 'Rival Analyst':
            pits = data['read_rival_pits']
            names = list(dict.fromkeys(p['name'] for p in pits if p['event']=='pit_entry'))
            summary = ('Recent stops: '+', '.join(names)+'.') if names else 'No rival pit entry observed in the last three minutes.'
            if not o.get('rivals',{}).get('available'):
                summary = 'Rival observations unavailable. Ask for review before relying on rival strategy.'
            neighbors = o.get('rivals',{}).get('nearby',[])[:3]
            if neighbors:
                summary += ' Historical neighbors: '+', '.join(r.get('name_acronym') or r.get('full_name') or str(r['driver_number']) for r in neighbors)+'.'
        elif self.role == 'Race Conditions & Incidents':
            damage = active_damage(b)
            summary = o['condition'].capitalize()+' track; race control '+o['race_control']+'.'
            if damage:
                summary += ' '+', '.join('Puncture' if i['type']=='puncture' else 'Broken front wing' for i in damage)+': modeled loss '+str(damage_penalty(b))+' seconds per lap until repaired.'
        else:
            summary = b['compound'].capitalize()+' tyres, '+str(b['tyre_age'])+' laps old. '+str(b['pit_count'])+' stops authorized by you.'
            if b['history'] and b['history'][-1].get('simulated_lap_seconds'):
                summary += ' Last simulated lap '+str(b['history'][-1]['simulated_lap_seconds'])+' seconds.'
        return {'role':self.role, 'as_of':o.get('as_of'), 'read_at':datetime.now(timezone.utc).isoformat(), 'lap':b['lap'], 'summary':summary, 'tools':data,
                'method':'Deterministic analytical specialist; Director supplies LLM reasoning.'}
