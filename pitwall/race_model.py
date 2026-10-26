"""Lap-synchronized relative-time race order and pit/recovery forecasts.

Rival gaps/pace are gated historical measurements. Leclerc's carried gap, pit
exit and catch estimates are simulation outputs, never historical outcomes.
"""
import math
import re
import statistics
from .data import parse_time
from .simulation import MODEL, penalty, damage_penalty, active_damage, default_compound

PIT=MODEL['pit_components']


def finite(value):return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value)


def gap_seconds(value,pace=100):
    if finite(value):return float(value)
    match=re.fullmatch(r'\+?(\d+)\s*LAPS?',str(value).strip(),re.I)
    return int(match[1])*pace if match else None


def leader_pace(observed):
    drivers=observed.get('rivals',{}).get('drivers',[])
    first=min(drivers,key=lambda r:r.get('position') or 99) if drivers else {}
    pace=first.get('pace_seconds') if 'pace_seconds' in first else first.get('last_lap_seconds')
    return pace if finite(pace) and 75<pace<180 else observed.get('pace_seconds') or 100.0


def base_pace(observed):
    baseline=observed.get('pace_seconds') or 100.0
    flag=observed['race_control']
    return max(baseline,leader_pace(observed),145 if flag=='SC' else 125) if flag in ('SC','VSC') else baseline


def seed_gap(observed):
    measured=gap_seconds(observed.get('historical_gap_to_leader'),leader_pace(observed))
    return measured if measured is not None else max(0,(observed.get('historical_position') or 1)-1)*.3


def stop_cost(branch,observed,repair_wing=True):
    flag=observed['race_control'];factor=PIT['sc_transit_factor'] if flag=='SC' else PIT['vsc_transit_factor'] if flag=='VSC' else 1.0
    wing=any(i['type']=='front_wing' for i in active_damage(branch)) and repair_wing
    puncture=any(i['type']=='puncture' for i in active_damage(branch))
    service=max(PIT['tyre_service_seconds'],PIT['wing_service_seconds'] if wing else 0)
    checks=PIT['puncture_checks_seconds'] if puncture else 0.0
    transit=PIT['lane_transit_seconds']*factor
    return {'race_control':flag,'transit_seconds':round(transit,2),'transit_factor':factor,
            'stationary_seconds':service,'tyre_service_seconds':PIT['tyre_service_seconds'],
            'wing_service_seconds':PIT['wing_service_seconds'] if wing else 0,'puncture_checks_seconds':checks,
            'traffic_seconds':PIT['traffic_seconds'],'total_loss_seconds':round(transit+service+checks+PIT['traffic_seconds'],2),
            'repair_wing':bool(wing),'fixes_puncture':puncture,
            'assumption':'Illustrative net pit transit and crew timings. Only transit is reduced by SC/VSC; service and checks still take full time.'}


def classification(branch,observed,extra_loss=0):
    own=branch.get('race_gap_seconds',seed_gap(observed))+extra_loss
    rivals=observed.get('rivals',{}).get('drivers',[])
    entries=[];missing=0
    for rival in rivals:
        gap=gap_seconds(rival.get('gap_to_leader_seconds'),leader_pace(observed))
        estimated=False
        if gap is None and branch['lap']==1 and rival.get('position'):
            gap=(rival['position']-1)*.3;estimated=True
        if gap is None:missing+=1;continue
        last=rival.get('last_lap_at')
        stale=max(0,(parse_time(observed['as_of'])-parse_time(last)).total_seconds()) if last and observed.get('as_of') else 0
        if branch['lap']>2 and stale>max(180,2.5*leader_pace(observed)):
            gap+=stale;estimated=True  # Stopped/retired or stale data cannot remain frozen near the front.
        entries.append({**rival,'race_gap_seconds':round(gap,3),'gap_estimated':estimated,'last_lap_age_seconds':round(stale,1),'timing_status':'stale; progress estimated' if estimated and branch['lap']>2 else 'recorded timing',
                        'gap_to_leclerc_seconds':round(gap-own,3),'source':'historical rival timing'})
    entries.append({'driver_number':16,'full_name':'Charles Leclerc','name_acronym':'LEC','team_name':'Ferrari',
                    'compound':branch['compound'],'tyre_age':branch['tyre_age'],'race_gap_seconds':round(own,3),
                    'gap_to_leclerc_seconds':0.0,'source':'simulated Leclerc'})
    entries.sort(key=lambda r:(r['race_gap_seconds'],r.get('position') or observed.get('historical_position') or 99))
    minimum=entries[0]['race_gap_seconds']
    for pos,row in enumerate(entries,1):
        row['simulated_position']=pos;row['gap_to_race_leader_seconds']=round(row['race_gap_seconds']-minimum,3)
    index=next(i for i,r in enumerate(entries) if r['driver_number']==16)
    ahead=entries[index-1] if index else None;behind=entries[index+1] if index+1<len(entries) else None
    return {'position':index+1 if len(entries)>1 else None,'ahead':ahead,'behind':behind,
            'gap_to_ahead_seconds':round(own-ahead['race_gap_seconds'],3) if ahead else None,
            'gap_to_behind_seconds':round(behind['race_gap_seconds']-own,3) if behind else None,
            'own_gap_seconds':round(own,3),'field':entries,'missing_rivals':missing,
            'quality':'partial' if missing or len(rivals)<19 else ('estimated grid gaps' if branch['lap']==1 else 'stale timing estimate') if any(r.get('gap_estimated') for r in entries) else 'lap-synchronized simulation',
            'as_of':observed.get('as_of'),'note':'Rival timing is historical; Leclerc position and gaps are simulated. Missing timing makes the rank provisional.'}


def pit_forecast(branch,observed,compound=None,repair_wing=True):
    chosen=compound or branch.get('planned_stop',{}).get('compound') or default_compound(observed)
    cost=stop_cost(branch,observed,repair_wing)
    current=classification(branch,observed)
    exit=classification(branch,observed,cost['total_loss_seconds'])
    remaining=max(0,branch.get('total_laps',56)-branch['lap'])
    horizon=min(8,remaining)
    target_pace=base_pace(observed)+penalty(chosen,0,observed['condition'])
    remaining_damage=sum(MODEL['damage_seconds_per_lap'][i['type']] for i in active_damage(branch) if i['type']=='front_wing' and not repair_wing)
    target_pace+=remaining_damage
    candidates=[]
    # A pit-ineligible flag cannot produce a valid executable forecast.
    available=not branch.get('finished',False) and observed['race_control']!='RED' and chosen in MODEL['degradation']
    for rival in exit['field']:
        if rival['driver_number']==16 or rival['gap_to_leclerc_seconds']>=0:continue
        rp=rival.get('pace_seconds') or rival.get('last_lap_seconds')
        if not finite(rp) or not 75<rp<115 or rival.get('representative_laps',3)<3:continue
        if sum(isinstance(r.get('seconds'),(int,float)) for r in observed.get('recent_laps',[]))<3:continue
        gap=-rival['gap_to_leclerc_seconds'];age=rival.get('tyre_age')
        if not finite(age):continue
        tyre=rival.get('compound','UNKNOWN')
        gain=0.0;catch=None;gains=[]
        for step in range(horizon):
            rival_lap=rp+penalty(tyre,age+step,observed['condition'])-penalty(tyre,age,observed['condition'])
            own_lap=base_pace(observed)+penalty(chosen,step,observed['condition'])+remaining_damage
            gain+=rival_lap-own_lap;gains.append(gain)
            if catch is None and gain>=gap+1.0:catch=step+1
        catchable=catch is not None and observed['race_control']=='GREEN' and available
        candidates.append({'driver_number':rival['driver_number'],'name':rival.get('full_name'),
            'gap_after_pit_seconds':round(gap,2),'observed_rival_pace_seconds':round(rp,3),
            'initial_pace_advantage_seconds':round(rp-target_pace,3),'estimated_catch_in_laps':catch if catchable else None,
            '_gains':gains,'catchable_in_horizon':catchable,'modeled_gain_in_horizon_seconds':round(gain,2)})
    candidates.sort(key=lambda r:r['gap_after_pit_seconds'])
    passing_delay=0.0;earliest=1;blocked=False
    for candidate in candidates:
        gains=candidate.pop('_gains')
        catch=next((i+1 for i,gain in enumerate(gains) if i+1>=earliest and gain>=candidate['gap_after_pit_seconds']+passing_delay+1.0),None)
        supported=not blocked and catch is not None and observed['race_control']=='GREEN' and available
        candidate['estimated_catch_in_laps']=catch if supported else None
        candidate['catchable_in_horizon']=supported
        if supported:passing_delay+=1.0;earliest=catch
        else:blocked=True
    potential=[r for r in candidates if r['catchable_in_horizon']]
    return {'as_of':observed.get('as_of'),'lap':branch['lap'],'compound':chosen,'available':available,
            'current_position':current['position'],'pit_exit_position':exit['position'] if available else None,
            'positions_lost':max(0,(exit['position'] or 0)-(current['position'] or 0)) if available else None,
            'rejoin_behind':exit['ahead'] if available else None,'rejoin_ahead_of':exit['behind'] if available else None,
            'gap_behind_seconds':exit['gap_to_ahead_seconds'] if available else None,
            'gap_ahead_seconds':exit['gap_to_behind_seconds'] if available else None,'stop_cost':cost,'cost_by_flag':{flag:stop_cost(branch,{**observed,'race_control':flag},repair_wing) for flag in ('GREEN','VSC','SC')},
            'fresh_tyre_pace_seconds':round(target_pace,3),'horizon_laps':horizon,
            'potential_overtakes':len(potential),'potential_targets':potential,'recovery_candidates':candidates,
            'potential_position_after_recovery':max(1,(exit['position'] or 1)-len(potential)) if available else None,
            'quality':exit['quality'],'note':'Estimated pit exit and pace-based catch opportunities. Current rival pace/strategy is held constant with modeled tyre wear. One second of cumulative passing delay is allowed per target and an uncatchable car blocks further recovery. Traffic, defending, future rival stops and weather may prevent passes. Passing is disabled under SC/VSC.'}
