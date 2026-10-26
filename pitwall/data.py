"""Import public observations with explicit knowledge availability timestamps."""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENDPOINTS = ('laps', 'stints', 'position', 'intervals', 'pit', 'weather', 'race_control')


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('Observation timestamps must include a timezone')
    return parsed.astimezone(timezone.utc)


def iso_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def digest(value) -> str:
    encoded = json.dumps(value, sort_keys=True, allow_nan=False, separators=(',', ':')).encode()
    return hashlib.sha256(encoded).hexdigest()


def build_timeline(raw: dict, project: dict) -> dict:
    session_key = project['race']['session_key']
    driver_number = project['driver']['driver_number']
    sessions = raw['sessions']
    if len(sessions) != 1 or sessions[0].get('session_key') != session_key:
        raise ValueError('Unexpected race session')
    drivers = raw['drivers']
    if len(drivers) != 1 or drivers[0].get('driver_number') != driver_number:
        raise ValueError('Unexpected controlled driver')
    for endpoint in ENDPOINTS:
        if endpoint not in raw or not isinstance(raw[endpoint], list):
            raise ValueError(f'Missing source dataset: {endpoint}')
        for row in raw[endpoint]:
            if row.get('session_key') != session_key:
                raise ValueError(f'Wrong session in {endpoint}')
            if endpoint not in ('weather', 'race_control') and row.get('driver_number') != driver_number:
                raise ValueError(f'Wrong driver in {endpoint}')
    if not raw['laps'] or not raw['stints']:
        raise ValueError('Lap and tyre-stint evidence is required')

    events = []
    dropped = []

    def add(kind, when, payload):
        events.append({'kind': kind, 'available_at': iso_time(parse_time(when)), 'payload': payload})

    lap_starts = {}
    for row in raw['laps']:
        if row.get('date_start'):
            lap_starts[row['lap_number']] = row['date_start']
        duration = row.get('lap_duration')
        # A complete lap record contains future information at its start time.
        # Make it available only after the lap finishes. Do not invent a finish
        # time for incomplete laps or expose their sector fields prematurely.
        if not row.get('date_start') or not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0:
            dropped.append({'endpoint': 'laps', 'lap_number': row.get('lap_number'), 'reason': 'No reliable completed-lap timestamp'})
            continue
        finished = parse_time(row['date_start']) + timedelta(seconds=duration)
        add('completed_lap', iso_time(finished), {k: row.get(k) for k in (
            'driver_number', 'lap_number', 'date_start', 'lap_duration',
            'duration_sector_1', 'duration_sector_2', 'duration_sector_3', 'is_pit_out_lap')})

    for row in raw['stints']:
        when = lap_starts.get(row.get('lap_start'))
        if when:
            # lap_end and final stint length are future facts, not observations
            # available when the compound is first observed.
            add('tyre_stint_started', when, {k: row.get(k) for k in (
                'driver_number', 'stint_number', 'lap_start', 'compound', 'tyre_age_at_start')})
        else:
            dropped.append({'endpoint': 'stints', 'stint_number': row.get('stint_number'), 'reason': 'Start time is unavailable'})

    fields = {
        'position': ('driver_number', 'position'),
        'intervals': ('driver_number', 'gap_to_leader', 'interval'),
        'weather': ('air_temperature', 'track_temperature', 'rainfall', 'humidity', 'wind_speed', 'wind_direction'),
        'race_control': ('category', 'flag', 'message', 'lap_number', 'driver_number', 'scope', 'sector'),
    }
    for endpoint, names in fields.items():
        for row in raw[endpoint]:
            if row.get('date'):
                add(endpoint, row['date'], {k: row.get(k) for k in names})
            else:
                dropped.append({'endpoint': endpoint, 'reason': 'Observation time is unavailable'})
    for row in raw['pit']:
        duration = row.get('lane_duration')
        if duration is None:
            duration = row.get('pit_duration')  # Compatibility with older exports.
        if row.get('date') and isinstance(duration, (int, float)) and math.isfinite(duration) and duration >= 0:
            add('pit_visit_completed', iso_time(parse_time(row['date']) + timedelta(seconds=duration)),
                {'driver_number': row.get('driver_number'), 'lap_number': row.get('lap_number'),
                 'lane_duration': duration, 'stop_duration': row.get('stop_duration')})
        else:
            dropped.append({'endpoint': 'pit', 'reason': 'Pit completion time is unavailable'})

    events.sort(key=lambda item: (item['available_at'], item['kind']))
    if not any(event['kind'] == 'completed_lap' for event in events):
        raise ValueError('No completed laps were imported')
    return {
        'schema_version': 1,
        'source': 'OpenF1 historical observations',
        'race': project['race'], 'driver': project['driver'],
        'start_at': iso_time(parse_time(sessions[0]['date_start'])),
        # Session metadata end times can differ from the actual event timeline.
        # This internal bound is not exposed by the agent observation interface.
        'end_at': max(events[-1]['available_at'], iso_time(parse_time(sessions[0]['date_end']))),
        'events': events,
        'dropped_records': dropped,
    }


def load_timeline(path: Path) -> dict:
    timeline = json.loads(path.read_text())
    if timeline.get('schema_version') != 1 or not timeline.get('events'):
        raise ValueError('Unsupported or empty replay dataset')
    start = parse_time(timeline['start_at'])
    end = parse_time(timeline['end_at'])
    if end < start:
        raise ValueError('Replay end precedes its start')
    previous = None
    for event in timeline['events']:
        available = parse_time(event['available_at'])
        if previous is not None and available < previous:
            raise ValueError('Replay observations must be in chronological order')
        if available > end or not isinstance(event.get('payload'), dict):
            raise ValueError('Invalid observation payload or replay boundary')
        previous = available
    return timeline
