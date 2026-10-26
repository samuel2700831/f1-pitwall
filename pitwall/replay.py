"""Durable replay cursor and a time-gated observation interface.

Only this interface should later be exposed to agents. Raw provider files and
the complete timeline remain private to the replay service.
"""
from __future__ import annotations

import copy
import json
import math
import re
import sqlite3
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

from .data import digest, iso_time, parse_time


class Replay:
    def __init__(self, database: Path, timeline: dict):
        self.timeline = copy.deepcopy(timeline)
        self.dataset_digest = digest(self.timeline)
        self.database = Path(database)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript('''
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    dataset_digest TEXT NOT NULL,
                    cursor TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    entry_lap INTEGER,
                    entry_boundary TEXT
                );
                CREATE TABLE IF NOT EXISTS actions (
                    run_id TEXT NOT NULL,
                    action_id TEXT NOT NULL,
                    parameters TEXT NOT NULL,
                    response TEXT NOT NULL,
                    PRIMARY KEY (run_id, action_id),
                    FOREIGN KEY (run_id) REFERENCES runs(run_id)
                );
                CREATE TABLE IF NOT EXISTS memory (
                    run_id TEXT NOT NULL,
                    memory_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    content TEXT NOT NULL,
                    as_of TEXT NOT NULL,
                    state_version INTEGER NOT NULL,
                    PRIMARY KEY (run_id, memory_id),
                    FOREIGN KEY (run_id) REFERENCES runs(run_id)
                );
            ''')
            # Keep existing Part 1 runs usable without resetting their history.
            columns = {row['name'] for row in connection.execute('PRAGMA table_info(runs)')}
            if 'entry_lap' not in columns:
                connection.execute('ALTER TABLE runs ADD COLUMN entry_lap INTEGER')
            if 'entry_boundary' not in columns:
                connection.execute('ALTER TABLE runs ADD COLUMN entry_boundary TEXT')

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.database, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys = ON')
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _run(self, connection, run_id):
        row = connection.execute('SELECT * FROM runs WHERE run_id = ?', (run_id,)).fetchone()
        if row is None:
            raise ValueError('Unknown replay run')
        if row['dataset_digest'] != self.dataset_digest:
            raise ValueError('Dataset changed; use the original dataset or start a new run')
        return dict(row)

    def _lap_cutoff(self, lap, boundary):
        if isinstance(lap, bool) or not isinstance(lap, int) or lap < 1:
            raise ValueError('Lap must be a positive integer')
        if boundary not in ('start', 'end'):
            raise ValueError('Choose the start or end of the lap')
        matches = [event for event in self.timeline['events']
                   if event['kind'] == 'completed_lap' and event['payload'].get('lap_number') == lap]
        if len(matches) != 1:
            raise ValueError('A reliable boundary is unavailable for the requested lap')
        # Read only the requested boundary from the archive. The lap's duration
        # and sectors still pass through the observation cutoff before exposure.
        value = matches[0]['payload'].get('date_start') if boundary == 'start' else matches[0]['available_at']
        if not value:
            raise ValueError('A reliable boundary is unavailable for the requested lap')
        cutoff = parse_time(value)
        if boundary == 'start' and lap > 1 and self.timeline.get('controlled_strategy_removed'):
            # OpenF1 lap-start and lap-duration clocks differ by milliseconds.
            # A checkpoint must include the lap that just finished, while
            # still withholding every completion of the newly started lap.
            previous = [e for e in self.timeline['events'] if e['kind']=='completed_lap'
                        and e['payload'].get('lap_number')==lap-1]
            if previous:
                cutoff=max(cutoff,parse_time(previous[0]['available_at']))
        if not parse_time(self.timeline['start_at']) <= cutoff <= parse_time(self.timeline['end_at']):
            raise ValueError('Requested lap is outside the replay timeline')
        return iso_time(cutoff)

    def create(self, run_id, *, lap=None, boundary='start'):
        if not isinstance(run_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', run_id):
            raise ValueError('Use a run ID of 1 to 100 letters, digits, hyphens or underscores')
        if lap is None and boundary != 'start':
            raise ValueError('A lap number is required to select a lap boundary')
        cursor = self.timeline['start_at'] if lap is None else self._lap_cutoff(lap, boundary)
        with self._connect() as connection:
            try:
                connection.execute('''INSERT INTO runs
                    (run_id, dataset_digest, cursor, version, status, entry_lap, entry_boundary)
                    VALUES (?, ?, ?, 0, ?, ?, ?)''',
                    (run_id, self.dataset_digest, cursor, 'ready', lap, boundary if lap is not None else None))
            except sqlite3.IntegrityError as error:
                raise ValueError('Run already exists; resume with status or step') from error
        return self.status(run_id)

    def jump_to_lap(self, run_id, lap, boundary='start', *, action_id=None, expected_version=None):
        """Start a scenario or move an existing scenario forward with its memory.

        Backward navigation requires a new run ID. It cannot bring later memory
        into an earlier historical situation.
        """
        cursor = self._lap_cutoff(lap, boundary)
        with self._connect() as connection:
            exists = connection.execute('SELECT 1 FROM runs WHERE run_id = ?', (run_id,)).fetchone()
        if not exists:
            return self.create(run_id, lap=lap, boundary=boundary)
        if action_id is None or expected_version is None:
            raise ValueError('Run already exists; provide an action ID and expected state version to continue it')
        parameters = {'operation': 'jump_to_lap', 'lap': lap, 'boundary': boundary}
        return self._advance(run_id, action_id, expected_version, parameters, lambda _row: parse_time(cursor))

    def _visible(self, cursor):
        cutoff = parse_time(cursor)
        return [event for event in self.timeline['events'] if parse_time(event['available_at']) <= cutoff]

    def _memory(self, connection, row):
        entries = connection.execute('''SELECT memory_id, kind, content, as_of, state_version
            FROM memory WHERE run_id = ? AND state_version <= ? ORDER BY state_version, memory_id''',
            (row['run_id'], row['version'])).fetchall()
        return [dict(entry) for entry in entries if parse_time(entry['as_of']) <= parse_time(row['cursor'])]

    def _snapshot(self, row, connection):
        visible = self._visible(row['cursor'])
        latest = {}
        for event in visible:
            latest[event['kind']] = event
        started_laps = [event['payload']['lap_number'] for event in self.timeline['events']
                        if event['kind'] == 'completed_lap' and event['payload'].get('date_start')
                        and parse_time(event['payload']['date_start']) <= parse_time(row['cursor'])]
        return {
            'run_id': row['run_id'], 'version': row['version'], 'status': row['status'],
            'objective': 'balanced', 'as_of': row['cursor'],
            'race': copy.deepcopy(self.timeline['race']),
            'driver': copy.deepcopy(self.timeline['driver']),
            'data_mode': 'historical_observations_only_no_strategy_branch_yet',
            'entry_point': ({'lap': row['entry_lap'], 'boundary': row['entry_boundary']}
                            if row.get('entry_lap') is not None else {'session_start': True}),
            'current_lap': max(started_laps) if started_laps else None,
            'memory': self._memory(connection, row),
            'latest_observations': copy.deepcopy(latest),
        }

    def status(self, run_id):
        with self._connect() as connection:
            return self._snapshot(self._run(connection, run_id), connection)

    def observations(self, run_id, through=None, kind=None):
        with self._connect() as connection:
            row = self._run(connection, run_id)
        cutoff = through or row['cursor']
        if parse_time(cutoff) > parse_time(row['cursor']):
            raise ValueError('Future observations are unavailable at the replay clock')
        return copy.deepcopy([event for event in self._visible(cutoff) if kind is None or event['kind'] == kind])

    def decision_context(self, run_id):
        """Prepare the bounded evidence packet for an on-demand agent assessment.

        This does not run an LLM or imply that a strategy has been calculated.
        No recommendations from a different replay run are incorporated.
        """
        with self._connect() as connection:
            row = self._run(connection, run_id)
            snapshot = self._snapshot(row, connection)
        visible = self._visible(row['cursor'])
        laps = [event for event in visible if event['kind'] == 'completed_lap']
        track_flags = [event for event in visible if event['kind'] == 'race_control'
                       and event['payload'].get('scope') == 'Track' and event['payload'].get('flag')]
        cutoff = parse_time(row['cursor'])
        return {
            'assessment_status': 'not_run_agents_not_connected',
            'trigger': 'manual_lap_assessment',
            'snapshot': snapshot,
            'recent_completed_laps': copy.deepcopy(laps[-5:]),
            'recent_race_control_messages': copy.deepcopy([event for event in visible if event['kind'] == 'race_control'][-10:]),
            'latest_observed_track_flag': copy.deepcopy(track_flags[-1]) if track_flags else None,
            'observation_age_seconds': {
                kind: max(0, (cutoff - parse_time(event['available_at'])).total_seconds())
                for kind, event in snapshot['latest_observations'].items()
            },
            'analysis_required': [
                'Evaluate pace, tyre condition and available traffic evidence',
                'Compare valid pit timing and tyre choices using the strategy model',
                'Challenge uncertain assumptions and choose action or human handoff',
            ],
            'limitations': [
                'Forward navigation retains recorded memory; strategy execution is not implemented yet',
                'Rival trajectories and tyre inventory are not imported yet',
                'Strategy simulation and LLM agents are not connected yet',
            ],
        }

    def step(self, run_id, seconds, action_id, expected_version):
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or not 0 < seconds <= 300:
            raise ValueError('A step must advance more than zero and at most 300 seconds')
        return self._advance(run_id, action_id, expected_version, {'seconds': float(seconds)},
                             lambda row: min(parse_time(row['cursor']) + timedelta(seconds=seconds),
                                             parse_time(self.timeline['end_at'])))

    @staticmethod
    def _validate_request(action_id, expected_version):
        if not isinstance(action_id, str) or not action_id.strip() or len(action_id) > 100:
            raise ValueError('An action ID of 1 to 100 characters is required for safe retries')
        if isinstance(expected_version, bool) or not isinstance(expected_version, int) or expected_version < 0:
            raise ValueError('An expected integer state version is required')

    def _advance(self, run_id, action_id, expected_version, parameters, get_cursor):
        self._validate_request(action_id, expected_version)
        operation = parameters.get('operation', 'step')
        parameters = json.dumps(parameters, sort_keys=True)
        with self._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = self._run(connection, run_id)
            previous = connection.execute('SELECT * FROM actions WHERE run_id = ? AND action_id = ?',
                                          (run_id, action_id)).fetchone()
            if previous:
                if previous['parameters'] != parameters:
                    raise ValueError('Action ID was already used with different parameters')
                # Return the original response even if its version has since
                # advanced, without rewinding the durable run state.
                return json.loads(previous['response'])
            if row['version'] != expected_version:
                raise ValueError('Stale state version; refresh before submitting a new action')
            cursor = get_cursor(row)
            if cursor < parse_time(row['cursor']):
                raise ValueError('Cannot jump backward inside this run; use a new run ID for an earlier scenario')
            if row['status'] == 'completed' and cursor != parse_time(row['cursor']):
                raise ValueError('Replay has reached the end of the available observations')
            if row['status'] == 'completed' and operation == 'step':
                raise ValueError('Replay has reached the end of the available observations')
            changed = cursor != parse_time(row['cursor'])
            if changed:
                row.update(cursor=iso_time(cursor), version=row['version'] + 1,
                           status='completed' if cursor >= parse_time(self.timeline['end_at']) else 'replaying')
            result = self._snapshot(row, connection)
            connection.execute('UPDATE runs SET cursor = ?, version = ?, status = ? WHERE run_id = ?',
                               (row['cursor'], row['version'], row['status'], run_id))
            connection.execute('INSERT INTO actions VALUES (?, ?, ?, ?)',
                               (run_id, action_id, parameters, json.dumps(result, allow_nan=False)))
        return result

    def remember(self, run_id, content, memory_id, expected_version, kind='note'):
        """Record durable context without implying a strategy was executed."""
        self._validate_request(memory_id, expected_version)
        if kind not in ('note', 'assumption', 'decision', 'human_override'):
            raise ValueError('Unsupported memory kind')
        if not isinstance(content, str) or not content.strip() or len(content) > 4000:
            raise ValueError('Memory must contain 1 to 4000 characters')
        with self._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = self._run(connection, run_id)
            previous = connection.execute('''SELECT memory_id, kind, content, as_of, state_version
                FROM memory WHERE run_id = ? AND memory_id = ?''', (run_id, memory_id)).fetchone()
            if previous:
                if previous['kind'] != kind or previous['content'] != content:
                    raise ValueError('Memory ID was already used with different content')
                return dict(previous)
            if row['version'] != expected_version:
                raise ValueError('Stale state version; refresh before recording new memory')
            version = row['version'] + 1
            entry = {'memory_id': memory_id, 'kind': kind, 'content': content,
                     'as_of': row['cursor'], 'state_version': version}
            connection.execute('INSERT INTO memory VALUES (?, ?, ?, ?, ?, ?)',
                               (run_id, memory_id, kind, content, row['cursor'], version))
            connection.execute('UPDATE runs SET version = ? WHERE run_id = ?', (version, run_id))
        return entry
