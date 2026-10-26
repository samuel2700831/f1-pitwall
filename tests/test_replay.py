import copy
import json
import tempfile
import unittest
import sqlite3
from pathlib import Path

from pitwall.data import build_timeline, parse_time, digest
from pitwall.replay import Replay


def source_fixture():
    """Small fabricated observations for tests, not a replay of actual Spa laps."""
    project = {'race': {'session_key': 123, 'year': 2025},
               'driver': {'driver_number': 16, 'name': 'Test driver'}}
    raw = {
        'sessions': [{'session_key': 123, 'date_start': '2025-07-27T13:00:00Z', 'date_end': '2025-07-27T13:04:00Z'}],
        'drivers': [{'driver_number': 16}],
        'laps': [
            {'session_key': 123, 'driver_number': 16, 'lap_number': 1, 'date_start': '2025-07-27T13:00:00Z', 'lap_duration': 90, 'duration_sector_1': 30},
            {'session_key': 123, 'driver_number': 16, 'lap_number': 2, 'date_start': '2025-07-27T13:01:30Z', 'lap_duration': 100, 'duration_sector_1': 32},
        ],
        'stints': [{'session_key': 123, 'driver_number': 16, 'lap_start': 1, 'lap_end': 2, 'stint_number': 1, 'compound': 'INTERMEDIATE', 'tyre_age_at_start': 0}],
        'position': [{'session_key': 123, 'driver_number': 16, 'date': '2025-07-27T13:00:00Z', 'position': 3}],
        'intervals': [],
        'pit': [{'session_key': 123, 'driver_number': 16, 'date': '2025-07-27T13:01:00Z', 'lap_number': 1, 'lane_duration': 25, 'stop_duration': 2.5}],
        'weather': [
            {'session_key': 123, 'date': '2025-07-27T13:00:00Z', 'rainfall': 1},
            {'session_key': 123, 'date': '2025-07-27T13:02:00Z', 'rainfall': 0},
        ],
        'race_control': [{'session_key': 123, 'date': '2025-07-27T13:03:00Z', 'message': 'FUTURE_ONLY_EVENT'}],
    }
    return project, raw


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'runs.sqlite3'
        self.project, self.raw = source_fixture()
        self.timeline = build_timeline(self.raw, self.project)
        self.replay = Replay(self.db, self.timeline)
        self.replay.create('run-a')

    def test_complete_lap_is_hidden_until_finish(self):
        self.replay.step('run-a', 89, 'tick-1', 0)
        self.assertEqual(self.replay.observations('run-a', kind='completed_lap'), [])
        self.replay.step('run-a', 1, 'tick-2', 1)
        laps = self.replay.observations('run-a', kind='completed_lap')
        self.assertEqual([event['payload']['lap_number'] for event in laps], [1])
        self.assertEqual(laps[0]['payload']['lap_duration'], 90)

    def test_future_conditions_and_messages_do_not_leak(self):
        self.replay.step('run-a', 100, 'tick', 0)
        events = self.replay.observations('run-a')
        self.assertNotIn('FUTURE_ONLY_EVENT', json.dumps(events))
        self.assertNotIn('FUTURE_ONLY_EVENT', json.dumps(self.replay.status('run-a')))
        self.assertEqual(self.replay.status('run-a')['latest_observations']['weather']['payload']['rainfall'], 1)
        with self.assertRaisesRegex(ValueError, 'Future'):
            self.replay.observations('run-a', through='2025-07-27T13:03:00Z')
        self.assertNotIn('end_at', self.replay.status('run-a'))

    def test_stint_endpoint_does_not_expose_its_future_end(self):
        event = self.replay.observations('run-a', kind='tyre_stint_started')[0]
        self.assertEqual(event['payload']['compound'], 'INTERMEDIATE')
        self.assertNotIn('lap_end', event['payload'])

    def test_pit_duration_waits_for_pit_completion(self):
        self.replay.step('run-a', 84, 'before-pit-completion', 0)
        self.assertEqual(self.replay.observations('run-a', kind='pit_visit_completed'), [])
        self.replay.step('run-a', 1, 'after-pit-completion', 1)
        self.assertEqual(self.replay.observations('run-a', kind='pit_visit_completed')[0]['payload']['lane_duration'], 25)

    def test_state_survives_a_new_process_instance(self):
        expected = self.replay.step('run-a', 90, 'tick', 0)
        resumed = Replay(self.db, self.timeline)
        self.assertEqual(resumed.status('run-a'), expected)

    def test_retried_action_does_not_execute_twice_or_rewind(self):
        first = self.replay.step('run-a', 90, 'request-1', 0)
        self.replay.step('run-a', 30, 'request-2', 1)
        self.assertEqual(self.replay.step('run-a', 90, 'request-1', 0), first)
        self.assertEqual(self.replay.status('run-a')['version'], 2)
        self.assertEqual(parse_time(self.replay.status('run-a')['as_of']).minute, 2)

    def test_action_id_cannot_be_reused_for_a_different_operation(self):
        self.replay.step('run-a', 90, 'same-request', 0)
        with self.assertRaisesRegex(ValueError, 'different parameters'):
            self.replay.step('run-a', 120, 'same-request', 1)
        self.assertEqual(self.replay.status('run-a')['version'], 1)

    def test_stale_state_is_rejected(self):
        self.replay.step('run-a', 30, 'first-request', 0)
        other_instance = Replay(self.db, self.timeline)
        with self.assertRaisesRegex(ValueError, 'Stale'):
            other_instance.step('run-a', 30, 'second-request', 0)
        self.assertEqual(other_instance.status('run-a')['version'], 1)

    def test_invalid_time_steps_leave_state_unchanged(self):
        original = self.replay.status('run-a')
        for value in (0, -1, 301, float('nan'), float('inf'), True, '60'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.replay.step('run-a', value, 'invalid', 0)
        self.assertEqual(self.replay.status('run-a'), original)

    def test_runs_keep_separate_cursors_and_action_ids(self):
        self.replay.create('run-b')
        self.replay.step('run-a', 90, 'same-id', 0)
        self.replay.step('run-b', 30, 'same-id', 0)
        self.assertNotEqual(self.replay.status('run-a')['as_of'], self.replay.status('run-b')['as_of'])

    def test_changed_dataset_cannot_silently_resume_old_state(self):
        changed = copy.deepcopy(self.timeline)
        changed['events'][0]['payload']['compound'] = 'MEDIUM'
        with self.assertRaisesRegex(ValueError, 'Dataset changed'):
            Replay(self.db, changed).status('run-a')

    def test_history_responses_cannot_mutate_the_underlying_dataset(self):
        events = self.replay.observations('run-a')
        events[0]['payload']['position'] = 999
        self.assertNotIn('999', json.dumps(self.replay.observations('run-a')))

    def test_end_is_clamped_and_new_actions_are_rejected_after_completion(self):
        result = self.replay.step('run-a', 300, 'last', 0)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(parse_time(result['as_of']), parse_time(self.timeline['end_at']))
        with self.assertRaisesRegex(ValueError, 'end of the available'):
            self.replay.step('run-a', 10, 'too-late', 1)

    def test_wrong_session_records_are_rejected(self):
        self.raw['weather'][0]['session_key'] = 999
        with self.assertRaisesRegex(ValueError, 'Wrong session'):
            build_timeline(self.raw, self.project)

    def test_incomplete_lap_is_omitted_and_reported(self):
        self.raw['laps'][1]['lap_duration'] = None
        imported = build_timeline(self.raw, self.project)
        self.assertEqual(len([event for event in imported['events'] if event['kind'] == 'completed_lap']), 1)
        self.assertEqual(imported['dropped_records'][0]['lap_number'], 2)

    def test_jump_to_lap_start_immediately_loads_only_prior_completed_laps(self):
        state = self.replay.jump_to_lap('lap-two', 2)
        self.assertEqual(state['as_of'], '2025-07-27T13:01:30+00:00')
        self.assertEqual(state['entry_point'], {'lap': 2, 'boundary': 'start'})
        self.assertEqual(state['version'], 0)
        laps = self.replay.observations('lap-two', kind='completed_lap')
        self.assertEqual([event['payload']['lap_number'] for event in laps], [1])
        self.assertNotIn('FUTURE_ONLY_EVENT', json.dumps(self.replay.decision_context('lap-two')))

    def test_jump_to_end_of_lap_includes_that_completed_lap(self):
        state = self.replay.jump_to_lap('end-two', 2, boundary='end')
        self.assertEqual(state['as_of'], '2025-07-27T13:03:10+00:00')
        self.assertEqual([event['payload']['lap_number'] for event in self.replay.observations('end-two', kind='completed_lap')], [1, 2])

    def test_jumping_back_creates_an_isolated_scenario_without_rewinding_existing_run(self):
        future = self.replay.step('run-a', 200, 'later-time', 0)
        self.assertIn('FUTURE_ONLY_EVENT', json.dumps(self.replay.decision_context('run-a')))
        self.replay.jump_to_lap('earlier-scenario', 1)
        self.assertNotIn('FUTURE_ONLY_EVENT', json.dumps(self.replay.decision_context('earlier-scenario')))
        self.assertEqual(self.replay.status('run-a'), future)
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM actions WHERE run_id = ?', ('earlier-scenario',)).fetchone()[0], 0)

    def test_lap_entry_and_context_survive_restart(self):
        expected = self.replay.create('at-two', lap=2)
        context = self.replay.decision_context('at-two')
        reopened = Replay(self.db, self.timeline)
        self.assertEqual(reopened.status('at-two'), expected)
        self.assertEqual(reopened.decision_context('at-two'), context)
        self.assertEqual(context['assessment_status'], 'not_run_agents_not_connected')

    def test_invalid_or_missing_lap_does_not_create_a_run(self):
        for lap in (0, -1, True, 1.5, '2', 3):
            with self.subTest(lap=lap), self.assertRaises(ValueError):
                self.replay.jump_to_lap('invalid-jump', lap)
        with self.assertRaisesRegex(ValueError, 'Unknown replay'):
            self.replay.status('invalid-jump')

    def test_invalid_boundary_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'start or end'):
            self.replay.jump_to_lap('invalid-boundary', 2, boundary='middle')

    def test_a_lap_selection_cannot_overwrite_an_existing_scenario(self):
        before = self.replay.status('run-a')
        with self.assertRaisesRegex(ValueError, 'already exists'):
            self.replay.jump_to_lap('run-a', 2)
        self.assertEqual(self.replay.status('run-a'), before)

    def test_context_has_evidence_timestamps_and_no_future_stint_end(self):
        self.replay.jump_to_lap('context-two', 2)
        context = self.replay.decision_context('context-two')
        self.assertGreaterEqual(context['observation_age_seconds']['weather'], 0)
        self.assertEqual(context['recent_completed_laps'][0]['payload']['lap_number'], 1)
        self.assertNotIn('lap_end', json.dumps(context))
        cutoff = parse_time(context['snapshot']['as_of'])
        for event in context['recent_completed_laps'] + context['recent_race_control_messages']:
            self.assertLessEqual(parse_time(event['available_at']), cutoff)

    def test_legacy_database_migration_preserves_saved_cursor(self):
        legacy_db = Path(self.temp.name) / 'legacy.sqlite3'
        with sqlite3.connect(legacy_db) as connection:
            connection.execute('CREATE TABLE runs (run_id TEXT PRIMARY KEY, dataset_digest TEXT NOT NULL, cursor TEXT NOT NULL, version INTEGER NOT NULL, status TEXT NOT NULL)')
            connection.execute('INSERT INTO runs VALUES (?, ?, ?, ?, ?)',
                               ('legacy', digest(self.timeline), '2025-07-27T13:01:00+00:00', 4, 'replaying'))
        upgraded = Replay(legacy_db, self.timeline)
        state = upgraded.status('legacy')
        self.assertEqual(state['version'], 4)
        self.assertEqual(state['as_of'], '2025-07-27T13:01:00+00:00')
        advanced = upgraded.step('legacy', 30, 'after-upgrade', 4)
        self.assertEqual(advanced['version'], 5)

    def test_forward_jump_carries_all_memory_kinds_into_strategy_context(self):
        self.replay.jump_to_lap('continuous', 1)
        for index, kind in enumerate(('note', 'assumption', 'decision', 'human_override')):
            self.replay.remember('continuous', 'Recorded ' + kind, 'memory-' + str(index), index, kind=kind)
        jumped = self.replay.jump_to_lap('continuous', 2, action_id='forward', expected_version=4)
        self.assertEqual(jumped['run_id'], 'continuous')
        self.assertEqual(jumped['current_lap'], 2)
        self.assertEqual(jumped['version'], 5)
        context = self.replay.decision_context('continuous')
        self.assertEqual([entry['kind'] for entry in context['snapshot']['memory']],
                         ['note', 'assumption', 'decision', 'human_override'])
        self.assertEqual(context['recent_completed_laps'][-1]['payload']['lap_number'], 1)
        self.assertNotIn('FUTURE_ONLY_EVENT', json.dumps(context))

    def test_forward_jump_adds_new_observations_without_overwriting_prior_memory(self):
        entry = self.replay.remember('run-a', 'Rain is observed now; reassess before choosing tyres', 'rain-assumption', 0, kind='assumption')
        jumped = self.replay.jump_to_lap('run-a', 2, boundary='end', action_id='forward', expected_version=1)
        self.assertEqual(jumped['latest_observations']['weather']['payload']['rainfall'], 0)
        self.assertEqual(jumped['memory'], [entry])
        self.assertLess(parse_time(entry['as_of']), parse_time(jumped['as_of']))

    def test_forward_jump_and_memory_survive_restart_together(self):
        self.replay.remember('run-a', 'Avoid an unnecessary extra stop', 'preference', 0, kind='human_override')
        self.replay.jump_to_lap('run-a', 2, action_id='forward', expected_version=1)
        expected = self.replay.decision_context('run-a')
        restarted = Replay(self.db, self.timeline)
        self.assertEqual(restarted.decision_context('run-a'), expected)
        self.assertEqual(restarted.status('run-a')['memory'][0]['content'], 'Avoid an unnecessary extra stop')

    def test_same_run_backward_jump_is_rejected_without_erasing_memory(self):
        self.replay.jump_to_lap('run-a', 2, action_id='forward', expected_version=0)
        self.replay.remember('run-a', 'Context learned on lap two', 'later-memory', 1)
        expected = self.replay.status('run-a')
        with self.assertRaisesRegex(ValueError, 'Cannot jump backward'):
            self.replay.jump_to_lap('run-a', 1, action_id='backward', expected_version=2)
        self.assertEqual(self.replay.status('run-a'), expected)
        earlier = self.replay.jump_to_lap('separate-earlier', 1)
        self.assertEqual(earlier['memory'], [])

    def test_duplicate_forward_jump_does_not_erase_memory_added_later(self):
        self.replay.remember('run-a', 'First note', 'first-note', 0)
        original = self.replay.jump_to_lap('run-a', 2, action_id='forward', expected_version=1)
        self.replay.remember('run-a', 'Second note', 'second-note', 2)
        retried = self.replay.jump_to_lap('run-a', 2, action_id='forward', expected_version=1)
        self.assertEqual(retried, original)
        self.assertEqual(self.replay.status('run-a')['version'], 3)
        self.assertEqual(len(self.replay.status('run-a')['memory']), 2)

    def test_memory_update_invalidates_a_stale_forward_jump(self):
        self.replay.remember('run-a', 'New human input', 'human-input', 0)
        with self.assertRaisesRegex(ValueError, 'Stale'):
            self.replay.jump_to_lap('run-a', 2, action_id='outdated-request', expected_version=0)
        self.assertEqual(self.replay.status('run-a')['version'], 1)

    def test_memory_retries_are_idempotent_and_conflicting_content_is_rejected(self):
        original = self.replay.remember('run-a', 'Hold the current plan', 'same-memory', 0, kind='decision')
        self.assertEqual(self.replay.remember('run-a', 'Hold the current plan', 'same-memory', 0, kind='decision'), original)
        with self.assertRaisesRegex(ValueError, 'different content'):
            self.replay.remember('run-a', 'Change the plan', 'same-memory', 1, kind='decision')
        self.assertEqual(self.replay.status('run-a')['version'], 1)
        self.assertEqual(len(self.replay.status('run-a')['memory']), 1)

    def test_memory_does_not_leak_between_runs_or_change_through_returned_objects(self):
        self.replay.remember('run-a', 'Private to this replay run', 'run-note', 0)
        state = self.replay.status('run-a')
        state['memory'][0]['content'] = 'Modified externally'
        self.assertEqual(self.replay.status('run-a')['memory'][0]['content'], 'Private to this replay run')
        self.replay.create('different-run')
        self.assertEqual(self.replay.status('different-run')['memory'], [])

    def test_invalid_memory_cannot_change_run_state(self):
        before = self.replay.status('run-a')
        for content, kind in [('', 'note'), ('x' * 4001, 'note'), ('a note', 'execute_pit_stop')]:
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                self.replay.remember('run-a', content, 'invalid-memory', 0, kind=kind)
        self.assertEqual(self.replay.status('run-a'), before)


if __name__ == '__main__':
    unittest.main()
