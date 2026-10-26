"""Small, inspectable counterfactual model. All coefficients are demo assumptions.

No final classification, future pace, rival trajectory, or future weather is used
to forecast. Historical conditions can be applied when the replay advances.
"""
from __future__ import annotations

import copy
import math
import statistics

from .data import parse_time

MODEL = {
    'version': 'austin-director-v5', 'pit_loss_seconds': 23.0,
    'horizon_laps': 8, 'traffic_cost_seconds': 2.0,
    'pit_components':{'lane_transit_seconds':20.5,'tyre_service_seconds':2.5,'wing_service_seconds':7.5,'puncture_checks_seconds':1.5,'traffic_seconds':2.0,'sc_transit_factor':.50,'vsc_transit_factor':.65},
    'damage_seconds_per_lap': {'front_wing': 4.0, 'puncture': 25.0},
    'wing_repair_seconds': 5.0,
    'degradation': {'SOFT': .16, 'MEDIUM': .10, 'HARD': .065, 'INTERMEDIATE': .20, 'WET': .22},
    'cliff_age': {'SOFT': 14, 'MEDIUM': 22, 'HARD': 32, 'INTERMEDIATE': 24, 'WET': 24},
    'fresh_offset': {'SOFT': -.6, 'MEDIUM': 0, 'HARD': .5, 'INTERMEDIATE': 0, 'WET': 1.5},
}
COMPOUNDS = tuple(MODEL['degradation'])


def evidence(events, lap, as_of):
    """Summarize an already time-gated observation list, never an archive."""
    latest = {event['kind']: event for event in events}
    weather = latest.get('weather')
    cutoff = parse_time(as_of)
    wet_recently = any(e['kind'] == 'weather' and e['payload'].get('rainfall') == 1
                       and (cutoff - parse_time(e['available_at'])).total_seconds() < 900 for e in events)
    rain = weather['payload'].get('rainfall') if weather else None
    condition = 'rain' if rain == 1 else 'drying' if rain == 0 and wet_recently else 'dry' if rain == 0 else 'unknown'
    control = 'UNKNOWN'
    for event in events:
        if event['kind'] != 'race_control':
            continue
        p = event['payload']; message = (p.get('message') or '').upper()
        if p.get('scope') == 'Track' and p.get('flag') in ('GREEN', 'RED', 'YELLOW', 'DOUBLE YELLOW') and 'PIT EXIT OPEN' not in message:
            control = p['flag']
        if 'ROLLING START' in message:
            control = 'GREEN'
        elif 'WILL START BEHIND THE SAFETY CAR' in message or 'WILL BE STARTED BEHIND THE SAFETY CAR' in message:
            control = 'SC'
        elif 'VIRTUAL SAFETY CAR DEPLOYED' in message:
            control = 'VSC'
        elif 'SAFETY CAR DEPLOYED' in message:
            control = 'SC'
        elif 'VIRTUAL SAFETY CAR ENDING' in message or 'VIRTUAL SAFETY CAR ENDED' in message or 'SAFETY CAR IN THIS LAP' in message:
            # Conservatively use a caution state until the next observed green.
            control = 'YELLOW'
        if p.get('scope')=='Track' and p.get('flag')=='CLEAR' and control=='YELLOW':
            control='GREEN'
        if p.get('category')=='SessionStatus' and 'SESSION STARTED' in message and control=='UNKNOWN':
            control='GREEN'
    stint = latest.get('tyre_stint_started', {}).get('payload', {})
    compound = stint.get('compound', 'UNKNOWN')
    age = max(0, lap - stint.get('lap_start', lap) + (stint.get('tyre_age_at_start') or 0))
    completed = [e for e in events if e['kind'] == 'completed_lap']
    recent = completed[-8:]
    # Do not mix neutralized laps with green-flag forecasts. An eight-lap
    # median also lags the falling fuel/pace baseline and double-counts wear.
    clean = [e['payload']['lap_duration'] for e in recent
             if not e['payload'].get('is_pit_out_lap') and 75 < (e['payload'].get('lap_duration') or 0) < 115][-3:]
    issues = []
    weather_age = (cutoff - parse_time(weather['available_at'])).total_seconds() if weather else None
    if weather_age is None or weather_age > 300:
        issues.append('Weather evidence is missing or more than five minutes old.')
    if condition in ('drying', 'unknown'):
        issues.append('Track grip is uncertain; rainfall does not measure surface grip.')
    if control != 'GREEN':
        issues.append('Race control is ' + control + '; routine green-flag assumptions may not apply.')
    if len(clean) < 3:
        issues.append('Fewer than three recent representative completed laps are available.')
    if compound not in COMPOUNDS:
        issues.append('The observed tyre compound is unavailable.')
    return {
        'lap': lap, 'as_of': as_of, 'historical_position': latest.get('position', {}).get('payload', {}).get('position'),
        'historical_interval': latest.get('intervals', {}).get('payload', {}).get('interval'),
        'historical_gap_to_leader':latest.get('intervals',{}).get('payload',{}).get('gap_to_leader'),
        'observed_compound': compound, 'observed_tyre_age': age,
        'condition': condition, 'rainfall': rain, 'race_control': control,
        'weather_age_seconds': round(weather_age, 1) if weather_age is not None else None,
        'track_temperature': weather['payload'].get('track_temperature') if weather else None,
        'air_temperature':weather['payload'].get('air_temperature') if weather else None,
        'humidity':weather['payload'].get('humidity') if weather else None,
        'wind_speed':weather['payload'].get('wind_speed') if weather else None,
        'pace_seconds': round(statistics.median(clean), 3) if clean else None,
        'recent_laps': [{'lap': e['payload']['lap_number'], 'seconds': e['payload']['lap_duration'],
                         'available_at': e['available_at']} for e in recent],
        'issues': issues,
        'evidence_refs': [{'kind': k, 'available_at': e['available_at']} for k, e in latest.items()],
        'recent_messages': [{'at': e['available_at'], 'message': e['payload'].get('message', '')}
                            for e in events if e['kind'] == 'race_control'][-5:],
    }


def new_branch(observed, lap):
    return {'simulation_version': MODEL['version'], 'incidents': [], 'condition_overrides': {},
            'acknowledged': {}, 'pause': None, 'target_lap': None, 'last_check_lap': lap, 'total_laps':observed.get('total_laps',44),
            'playback':{'status':'ready','speed':1,'next_at':None}, 'telemetry':None,
            'race_gap_seconds':observed.get('historical_gap_to_leader') if isinstance(observed.get('historical_gap_to_leader'),(int,float)) else max(0,(observed.get('historical_position') or 1)-1)*.3,
            'elapsed_delta_seconds':0.0,
            'trigger_state':{}, 'lap': lap, 'entry_lap': lap, 'compound': observed['observed_compound'],
            'tyre_age': observed['observed_tyre_age'], 'reference_compound': observed['observed_compound'],
            'reference_age': observed['observed_tyre_age'], 'delta_seconds': 0.0,
            'pit_count': 0, 'queued_pit': None, 'finished': False,
            'history': [], 'assessment': None, 'plan': 'You control every pit stop. Ask the Director or advance to the next decision.'}


def penalty(compound, age, condition, factor=1):
    if compound not in COMPOUNDS:
        # Invalid observed tyres must be repaired by human selection, not hidden.
        return 0.0
    deg = MODEL['degradation'][compound] * factor
    result = MODEL['fresh_offset'][compound] + deg * age + .025 * max(0, age - MODEL['cliff_age'][compound]) ** 2 * factor
    if condition == 'rain' and compound in ('SOFT', 'MEDIUM', 'HARD'):
        result += 18
    elif condition == 'dry' and compound in ('INTERMEDIATE', 'WET'):
        result += 8
    elif condition == 'drying' and compound in ('SOFT', 'MEDIUM', 'HARD'):
        result += 4
    return result


def default_compound(observed):
    return 'INTERMEDIATE' if observed['condition'] in ('rain', 'drying') else 'MEDIUM'


def pit_loss(observed, pit_seconds=None):
    parts=MODEL['pit_components'];base=pit_seconds if pit_seconds is not None else MODEL['pit_loss_seconds']
    factor=parts['sc_transit_factor'] if observed['race_control']=='SC' else parts['vsc_transit_factor'] if observed['race_control']=='VSC' else 1
    return (base-parts['tyre_service_seconds'])*factor+parts['tyre_service_seconds']


def active_damage(branch):
    return [i for i in branch.get('incidents', []) if i.get('active') and i['type'] in MODEL['damage_seconds_per_lap']]


def damage_penalty(branch):
    return sum(MODEL['damage_seconds_per_lap'][i['type']] for i in active_damage(branch))


def apply_conditions(observed, branch):
    result = copy.deepcopy(observed)
    overrides = branch.get('condition_overrides', {})
    result.update({k:v for k,v in overrides.items() if k in ('condition','race_control')})
    result['simulated_overrides'] = overrides
    return result


def decision_trigger(branch, observed):
    """Cheap monitor at each crossed lap; only the paused checkpoint calls the LLM."""
    lap = branch['lap']
    queue = branch.get('queued_pit')
    if queue and queue['lap'] <= lap:
        return None  # Execute an already authorized stop before checking again.
    triggers = []
    if branch['compound'] not in COMPOUNDS:
        triggers.append(('unknown-tyres','The current tyre compound is missing. Review before advancing.'))
    if observed['condition']=='unknown' or observed.get('weather_age_seconds') is None or observed.get('weather_age_seconds',0)>300:
        triggers.append(('weather-quality','Weather evidence is missing or stale. Review the uncertainty before advancing.'))
    for incident in active_damage(branch):
        triggers.append((incident['id'], 'Puncture: tyre change needed.' if incident['type']=='puncture' else 'Front-wing damage is costing pace. Review a repair stop.'))
    mismatch = ((observed['condition']=='dry' and branch['compound'] in ('INTERMEDIATE','WET')) or
                (observed['condition']=='rain' and branch['compound'] in ('SOFT','MEDIUM','HARD')))
    if mismatch:
        triggers.append(('tyres-'+observed['condition']+'-'+branch['compound'], 'Your tyres do not suit the current weather.'))
    if observed['race_control'] in ('SC','VSC','RED'):
        triggers.append(('flag-'+observed['race_control'], 'Race control: '+observed['race_control']+'. Review the pit window.'))
    nearby = {r['driver_number'] for r in observed.get('rivals',{}).get('nearby',[])}
    for pit in observed.get('rivals',{}).get('recent_pits',[]):
        if pit['event']=='pit_entry' and pit['driver_number'] in nearby:
            triggers.append(('rival-'+str(pit['driver_number'])+'-'+pit['at'], pit['name']+' is boxing. Review the nearby rival threat.'))
    cliff=MODEL['cliff_age'].get(branch['compound'],999)
    if branch['tyre_age'] >= cliff-2 and branch.get('total_laps',44)-lap>2:
        triggers.append(('tyre-dropoff','Tyres are approaching their modeled drop-off. Review a fresh-tyre stop.'))
    undercut=undercut_options(branch,observed)
    if undercut:
        triggers.append(('undercut-'+str(undercut[0]['driver_number']),'Undercut opportunity to review against '+undercut[0]['name']+'.'))
    report = compare(branch, observed)
    if report['options'] and report['options'][0]['id'] != 'hold':
        triggers.append(('pit-window', 'The model now favors a pit stop over staying out.'))
    report=compare(branch,observed)
    options={o['id']:o for o in report['options']}
    saving=-options.get('pit_now',{}).get('delta_to_hold_seconds',0)
    urgent=bool(active_damage(branch)) or mismatch or observed['race_control']=='RED'
    for key, reason in triggers:
        if key.startswith(('flag-','rival-','undercut-','tyre-dropoff','pit-window')) and not urgent and saving<2.0:
            continue  # Information alone is not a request to change strategy.
        last=branch.get('acknowledged',{}).get(key)
        prior=branch.get('trigger_state',{}).get(key)
        band=max(0,int(saving//10))
        if last is None and (prior is None or band>prior.get('band',0)):
            return {'lap':lap,'key':key,'reason':reason,'saving_seconds':round(saving,2),'band':band,'urgent':urgent}
        if last is not None and lap>last and prior is not None and band>prior.get('band',0):
            return {'lap':lap,'key':key,'reason':reason+' The modeled benefit has materially increased.','saving_seconds':round(saving,2),'band':band,'urgent':urgent}

    return None


def advance_lap(branch, observed, *, resolved=None):
    """Mutate one lap; consume a human stop once.

    The replay engine may supply the just-completed lap's neutral pace and
    clock reference. Those are realized conditions, not inputs to pre-lap
    forecasts. Carry the elapsed difference from that reference, including
    every simulated stop/damage penalty, instead of integrating independently
    smoothed Leclerc/leader pace estimates into an ever-drifting race gap.
    """
    lap = branch['lap']
    if branch['compound'] not in COMPOUNDS:
        raise ValueError('Choose a known tyre compound before advancing this branch')
    if observed['race_control'] == 'RED':
        raise ValueError('Race is suspended at this boundary. Open a later fresh scenario to resume safely.')
    from .race_model import stop_cost,base_pace,leader_pace
    cost = 0.0
    stopped = False
    queue = branch['queued_pit']
    if queue and queue.get('authorized_by') != 'human':
        raise ValueError('A pit stop requires explicit human authorization')
    repaired = []
    pit_profile=None
    if queue and queue['lap'] <= lap:
        pit_profile=stop_cost(branch,observed,queue.get('repair_wing',True))
        cost += pit_profile['total_loss_seconds']
        for incident in active_damage(branch):
            if incident['type'] == 'puncture' or queue.get('repair_wing', False):
                incident['active'] = False; incident['resolved_lap'] = lap
                repaired.append(incident['type'])
        branch['compound'] = queue['compound']; branch['tyre_age'] = 0
        branch['pit_count'] += 1; branch['queued_pit'] = None; stopped = True
        incident_ids={i['id'] for i in active_damage(branch)}
        branch['acknowledged']={k:v for k,v in branch.get('acknowledged',{}).items() if k in incident_ids}
        branch['trigger_state']={k:v for k,v in branch.get('trigger_state',{}).items() if k in incident_ids}
    cost += penalty(branch['compound'], branch['tyre_age'], observed['condition']) + damage_penalty(branch)
    reference = penalty(branch['reference_compound'], branch['reference_age'], observed['condition'])
    simulated_pace=(resolved['neutral_pace_seconds'] if resolved else base_pace(observed))+cost
    if resolved:
        branch['elapsed_delta_seconds']=round(branch.get('elapsed_delta_seconds',0)+simulated_pace-resolved['recorded_lap_seconds'],3)
        branch['race_gap_seconds']=round(resolved['reference_gap_seconds']+branch['elapsed_delta_seconds'],3)
    else:
        branch['race_gap_seconds']=round(branch.get('race_gap_seconds',0)+simulated_pace-max(leader_pace(observed),base_pace(observed) if observed['race_control'] in ('SC','VSC') else 0),3)
    branch['delta_seconds'] = round(branch['delta_seconds'] + cost - reference, 3)
    branch['history'].append({'lap': lap, 'compound': branch['compound'], 'tyre_age': branch['tyre_age'],
                              'pit': stopped, 'delta_seconds': branch['delta_seconds'],
                              'condition': observed['condition'], 'race_control': observed['race_control'],
                              'damage_seconds':damage_penalty(branch), 'repaired':repaired,
                              'simulated_lap_seconds':round(simulated_pace,3), 'pace_is_estimated':resolved['pace_is_estimated'] if resolved else not bool(observed.get('pace_seconds')),
                              'timing_method':'completed-lap reference plus carried elapsed difference' if resolved else 'pace estimate',
                              'race_gap_seconds':branch['race_gap_seconds'],'stop_cost':pit_profile})
    branch['tyre_age'] += 1; branch['reference_age'] += 1; branch['lap'] += 1


def compare(branch, observed, stress='baseline'):
    if stress not in ('baseline', 'conservative', 'aggressive'):
        raise ValueError('Unknown stress scenario')
    horizon = min(MODEL['horizon_laps'], max(0, branch.get('total_laps',44)+1 - branch['lap']))
    if branch['finished']:
        horizon = 0
    factor = {'baseline': 1.0, 'conservative': .65, 'aggressive': 1.4}[stress]
    from .race_model import stop_cost
    chosen = branch.get('planned_stop',{}).get('compound') or default_compound(observed)
    options = []
    choices = [('hold', 'Stay out', None), ('pit_now', 'Pit this lap', 0), ('pit_in_2', 'Pit in two laps', 2)]
    for key, label, offset in choices:
        if offset is not None and (offset >= horizon or observed['race_control'] == 'RED' or branch['compound'] not in COMPOUNDS):
            continue
        tyre, age, total = branch['compound'], branch['tyre_age'], 0.0
        damage = damage_penalty(branch)
        for step in range(horizon):
            if step == offset:
                tyre, age = chosen, 0
                repair=branch.get('planned_stop',{}).get('repair_wing',True)
                damage=sum(MODEL['damage_seconds_per_lap'][i['type']] for i in active_damage(branch) if i['type']=='front_wing' and not repair)
                total += stop_cost(branch,observed,repair)['total_loss_seconds']+(3 if stress=='conservative' else -3 if stress=='aggressive' else 0)
            total += penalty(tyre, age, observed['condition'], factor) + damage; age += 1
        options.append({'id': key, 'label': label, 'compound': chosen if offset is not None else branch['compound'],
                        'pit_lap': branch['lap'] + offset if offset is not None else None, 'cost_seconds': round(total, 3)})
    if not options:
        return {'horizon_laps': 0, 'options': [], 'stress': stress}
    hold_cost = options[0]['cost_seconds']
    for option in options:
        option['delta_to_hold_seconds'] = round(option['cost_seconds'] - hold_cost, 2)
    return {'horizon_laps': horizon, 'options': sorted(options, key=lambda o: o['cost_seconds']), 'stress': stress,
            'forecast_assumption': 'Current conditions persist. Pit options include damage repair; no future weather or rival response is forecast.'}


def undercut_options(branch, observed):
    """An evidence-led opportunity screen, not a predicted overtake."""
    if observed['condition']!='dry' or observed['race_control']!='GREEN' or branch['tyre_age']<6:
        return []
    candidates=[]
    for rival in observed.get('rivals',{}).get('nearby',[]):
        gap=rival.get('relative_gap_seconds');age=rival.get('tyre_age')
        if not isinstance(gap,(int,float)) or not -5<=gap<0 or not isinstance(age,(int,float)) or age<8:
            continue
        own_gain=max(0,penalty(branch['compound'],branch['tyre_age'],observed['condition'])-penalty(default_compound(observed),0,observed['condition']))
        rival_gain=max(0,penalty(rival.get('compound','UNKNOWN'),age,observed['condition'])-penalty(default_compound(observed),0,observed['condition']))
        candidates.append({'driver_number':rival['driver_number'],'name':rival.get('full_name',str(rival['driver_number'])),
            'historical_gap_ahead_seconds':round(-gap,3),'rival_tyre_age':age,
            'estimated_fresh_tyre_gain_seconds_per_lap':round(rival_gain,2),
            'own_fresh_tyre_gain_seconds_per_lap':round(own_gain,2),
            'note':'Consider pitting before this nearby historical rival. Pit loss, traffic and the rival response can reverse the benefit; no pass is guaranteed.'})
    return sorted(candidates,key=lambda c:c['historical_gap_ahead_seconds'])


def challenge(branch, observed):
    base = compare(branch, observed)
    conservative = compare(branch, observed, 'conservative')
    aggressive = compare(branch, observed, 'aggressive')
    for option in base['options']:
        values = [next(o['delta_to_hold_seconds'] for o in report['options'] if o['id'] == option['id'])
                  for report in (base, conservative, aggressive)]
        option['range_seconds'] = [min(values), max(values)]
    best = base['options'][0] if base['options'] else None
    runners = base['options'][1:]
    stable = all(report['options'] and report['options'][0]['id'] == best['id'] for report in (conservative, aggressive)) if best else False
    margin = runners[0]['cost_seconds'] - best['cost_seconds'] if runners else 0
    reasons = list(observed['issues'])
    if active_damage(branch):
        reasons.append('Car damage remains active until you authorize the appropriate repair stop.')
    if not stable or margin < 4:
        reasons.append('Pit choices are sensitive to degradation and pit-loss assumptions.')
    if best and best['id'] != 'hold':
        reasons.append('A pit changes the strategy; actual tyre inventory and rival responses are not modeled.')
    if branch.get('queued_pit'):
        reasons.append('A saved pit instruction is queued. Confirm any change to that instruction.')
    return {'comparison': base, 'recommended': best['id'] if best else 'hold',
            'needs_human': bool(reasons), 'reasons': reasons,
            'undercut_opportunities':undercut_options(branch,observed), 'robust_winner': stable, 'margin_seconds': round(margin, 2),
            'limitations': ['Sensitivity ranges are modeled scenarios, not statistical confidence intervals.',
                            'Demo tyre sets are assumed available. Sporting tyre rules and rival responses are not validated.',
                            'No simulated finishing position is claimed. Delta compares against holding entry tyres.']}
