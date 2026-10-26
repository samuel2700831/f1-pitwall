"""Transactional web use cases: durable branches, jobs, memory, and decisions."""
from __future__ import annotations

import copy
import json
import re
import uuid
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from .agents import make_client, StrategyWorkflow
from .replay import Replay
from .simulation import COMPOUNDS, MODEL, advance_lap, evidence, new_branch, apply_conditions, decision_trigger, active_damage, penalty, damage_penalty
from .rivals import RivalFeed
from .data import ROOT,parse_time
from .specialists import Specialist, ROLE_TOOLS
from .race_model import classification,pit_forecast,stop_cost,base_pace
from .evaluation import CASES, TARGETS, evaluate


def encoded(value):
    return json.dumps(value, allow_nan=False, separators=(',', ':'))


def now():
    return datetime.now(timezone.utc).isoformat()


class Conflict(ValueError):
    pass


class PitwallService:
    def __init__(self, database, timeline, *, client=None, workers=2, rivals=None, autoplay=False):
        self.replay = Replay(database, timeline)
        self.client = client or make_client()
        self.total_laps=timeline['race'].get('laps') or max(e['payload']['lap_number'] for e in timeline['events'] if e['kind']=='completed_lap')
        slug=timeline['race'].get('slug','spa-2025')
        path = ROOT/'data'/(slug+'-rivals.json')
        self.rivals = RivalFeed(rivals if rivals is not None else json.loads(path.read_text()) if path.exists() else None)
        self.executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix='pitwall')
        with self.replay._connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS web_branches (
                    run_id TEXT PRIMARY KEY REFERENCES runs(run_id), owner TEXT NOT NULL,
                    label TEXT NOT NULL, state TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS web_receipts (
                    run_id TEXT NOT NULL REFERENCES runs(run_id), request_id TEXT NOT NULL,
                    parameters TEXT NOT NULL, response TEXT NOT NULL,
                    PRIMARY KEY(run_id, request_id)
                );
                CREATE TABLE IF NOT EXISTS web_jobs (
                    job_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(run_id),
                    status TEXT NOT NULL, source_version INTEGER NOT NULL, mode TEXT NOT NULL,
                    trace TEXT NOT NULL, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    source_lap INTEGER, provider TEXT, model TEXT
                );
                CREATE TABLE IF NOT EXISTS agent_tests (
                    test_id TEXT PRIMARY KEY, owner TEXT NOT NULL, run_id TEXT NOT NULL,
                    source_version INTEGER NOT NULL, parameters TEXT NOT NULL,
                    snapshot TEXT NOT NULL, status TEXT NOT NULL, result TEXT,
                    error TEXT, created_at TEXT NOT NULL
                );
            ''')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(web_jobs)')}
            if 'source_lap' not in columns:
                db.execute('ALTER TABLE web_jobs ADD COLUMN source_lap INTEGER')
            for column in ('provider','model'):
                if column not in columns:
                    db.execute('ALTER TABLE web_jobs ADD COLUMN '+column+' TEXT')
            db.execute("UPDATE web_jobs SET status='interrupted', error=?, updated_at=? WHERE status IN ('queued','running')",
                       ('Server restarted during assessment. Your previous branch is unchanged; retry the assessment.', now()))
            db.execute("UPDATE agent_tests SET status='interrupted',error=? WHERE status IN ('queued','running')",
                       ('Server restarted during this test. Start a new test; the race is unchanged.',))

            # Restart never silently resumes a clock while the human is away.
            for saved in db.execute('SELECT b.run_id,b.state FROM web_branches b JOIN runs r ON b.run_id=r.run_id WHERE r.dataset_digest=?',(self.replay.dataset_digest,)).fetchall():
                state=json.loads(saved['state'])
                if state.get('playback',{}).get('status')=='running':
                    state['playback'].update(status='interrupted',next_at=None)
                    db.execute('UPDATE web_branches SET state=? WHERE run_id=?',(encoded(state),saved['run_id']))
        self._stop=threading.Event()
        self._clock=threading.Thread(target=self._ticker,name='pitwall-race-clock',daemon=True) if autoplay else None
        if self._clock:self._clock.start()

    def close(self):
        self._stop.set()
        if self._clock:self._clock.join(timeout=2)
        self.executor.shutdown(wait=True, cancel_futures=True)

    def meta(self):
        return {'race': self.replay.timeline['race'], 'driver': self.replay.timeline['driver'],
                'laps': self.total_laps, 'llm_available': self.client.configured, 'model': self.client.model,
                'llm_provider':self.client.provider,'provider_name':self.client.provider_name,
                'model_assumptions': MODEL, 'compounds': COMPOUNDS,
                'source': 'OpenF1 historical observations', 'simulation_version': MODEL['version'],
                'rivals_available':bool(self.rivals.data['events']), 'rivals_sha256':self.rivals.digest,
                'agents':['Rival Analyst','Race Conditions & Incidents','Leclerc Performance','Strategy Director'],
                'agent_test_cases':CASES,'agent_test_targets':TARGETS}

    def test_agents(self,owner,run_id,request_id,version,case='current',target='all',mode='local'):
        self._request(request_id,version)
        if version is None:raise ValueError('Expected version is required')
        if case not in {c['id'] for c in CASES} or target not in TARGETS or mode not in ('local','llm'):
            raise ValueError('Unknown test scenario, target or mode')
        if mode=='llm' and target in ('all','Strategy Director') and not self.client.configured:
            raise ValueError('LLM is not configured. Use local tests or configure Gemini.')
        test_id='test-'+request_id
        parameters=encoded({'run_id':run_id,'version':version,'case':case,'target':target,'mode':mode})
        with self.replay._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT owner,parameters FROM agent_tests WHERE test_id=?',(test_id,)).fetchone()
            if old:
                if old['owner']!=owner:raise KeyError('Test not found')
                if old['parameters']!=parameters:raise Conflict('Request ID already used with different test inputs')
                return self._test_view(db,owner,test_id)
            row,record,branch=self._owned(db,owner,run_id)
            if row['version']!=version:raise Conflict('Checkpoint changed; refresh before testing')
            if branch.get('simulation_version')!=MODEL['version']:raise Conflict('Start a new race to test the current model')
            if db.execute("SELECT 1 FROM agent_tests WHERE owner=? AND status IN ('queued','running')",(owner,)).fetchone():
                raise Conflict('Wait for your current agent test to finish')
            snapshot={'observed':self._current(branch,row['cursor']),'branch':branch,'memory':self.replay._memory(db,row)}
            db.execute('INSERT INTO agent_tests VALUES (?,?,?,?,?,?,?,?,?,?)',
                       (test_id,owner,run_id,version,parameters,encoded(snapshot),'queued',None,None,now()))
            result=self._test_view(db,owner,test_id)
        self.executor.submit(self._test_work,owner,test_id)
        return result

    @staticmethod
    def _test_view(db,owner,test_id):
        row=db.execute('SELECT * FROM agent_tests WHERE test_id=? AND owner=?',(test_id,owner)).fetchone()
        if not row:raise KeyError('Test not found in this browser session')
        result={k:row[k] for k in ('test_id','run_id','source_version','status','error','created_at')}
        result['parameters']=json.loads(row['parameters'])
        result['result']=json.loads(row['result']) if row['result'] else None
        return result

    def get_agent_test(self,owner,test_id):
        with self.replay._connect() as db:return self._test_view(db,owner,test_id)

    def _test_work(self,owner,test_id):
        try:
            with self.replay._connect() as db:
                row=db.execute('SELECT snapshot,parameters FROM agent_tests WHERE test_id=? AND owner=?',(test_id,owner)).fetchone()
                snapshot=json.loads(row['snapshot']);params=json.loads(row['parameters'])
                db.execute("UPDATE agent_tests SET status='running' WHERE test_id=?",(test_id,))
            result=evaluate(**snapshot,case=params['case'],target=params['target'],mode=params['mode'],client=self.client)
            with self.replay._connect() as db:
                db.execute("UPDATE agent_tests SET status='completed',result=? WHERE test_id=?",(encoded(result),test_id))
        except Exception:
            with self.replay._connect() as db:
                db.execute("UPDATE agent_tests SET status='failed',error=? WHERE test_id=?",
                           ('Test could not finish. The race was not changed; retry with a new test.',test_id))

    def _owned(self, db, owner, run_id):
        branch = db.execute('SELECT * FROM web_branches WHERE run_id=? AND owner=?', (run_id, owner)).fetchone()
        if not branch:
            raise KeyError('Run not found in this browser session')
        row = self.replay._run(db, run_id)
        return row, dict(branch), json.loads(branch['state'])

    def observed(self, lap, boundary='start'):
        cutoff = self.replay._lap_cutoff(lap, boundary)
        return self._evidence(lap, cutoff)

    def _evidence(self, lap, cutoff):
        result = evidence(self.replay._visible(cutoff), lap, cutoff)
        result['total_laps']=self.total_laps
        result['rivals'] = self.rivals.snapshot(cutoff, result['historical_position'],result.get('historical_gap_to_leader'))
        return result

    def _current(self, branch, cursor):
        return self._race_context(branch,apply_conditions(self._evidence(branch['lap'],cursor),branch))

    def _resolve_lap(self, branch, observed):
        """Resolve only a crossed lap; never expose its future pace to agents.

        Subtract the recorded clock duration to remove reference pit time as
        well as reference tyre effects. Only our human-authorized stops enter
        simulated elapsed time. The original clock is a coordinate system,
        not a command to execute the historical strategy.
        """
        if not self.replay.timeline.get('controlled_strategy_removed'):
            return None
        lap=branch['lap']
        cutoff=self.replay._lap_cutoff(lap if lap==self.total_laps else lap+1,
                                       'end' if lap==self.total_laps else 'start')
        events=self.replay._visible(cutoff)
        completed=next(e for e in events if e['kind']=='completed_lap' and e['payload']['lap_number']==lap)
        p=completed['payload']
        recorded=(parse_time(completed['available_at'])-parse_time(p['date_start'])).total_seconds()
        neutral=p.get('lap_duration')
        estimated=neutral is None
        if lap==1 and neutral is None:
            # Preserve the measured launch lap without treating its launch
            # loss as the ongoing pace, or inventing an opening 100-second lap.
            neutral=recorded-penalty(observed['observed_compound'],observed['observed_tyre_age'],'dry')
            estimated=False
        if neutral is None:
            neutral=base_pace(observed)  # causal estimate for pit-affected/missing clean pace
        following=self._evidence(lap if lap==self.total_laps else lap+1,cutoff)
        natural=self.observed(lap)
        if observed['race_control'] in ('SC','VSC') and observed['race_control']!=natural['race_control']:
            # An injected global caution slows the clock for the whole field,
            # not Leclerc alone. Do not charge him its slowdown as lost gap.
            slowed=max(neutral,145 if observed['race_control']=='SC' else 125)
            recorded+=slowed-neutral
            neutral=slowed
            estimated=True
        reference_gap=following.get('historical_gap_to_leader')
        if not isinstance(reference_gap,(int,float)):
            from .race_model import leader_pace
            reference_gap=branch.get('race_gap_seconds',0)-branch.get('elapsed_delta_seconds',0)+recorded-leader_pace(observed)
        return {'neutral_pace_seconds':neutral,'recorded_lap_seconds':recorded,
                'reference_gap_seconds':reference_gap,'pace_is_estimated':estimated}

    def _race_context(self,branch,observed):
        result=copy.deepcopy(observed)
        order=classification(branch,result)
        by_number={r['driver_number']:r for r in order['field']}
        for driver in result['rivals']['drivers']:
            simulated=by_number.get(driver['driver_number'],{})
            driver.setdefault('historical_relative_gap_seconds',driver.get('relative_gap_seconds'))
            driver['relative_gap_seconds']=simulated.get('gap_to_leclerc_seconds')
            driver['simulated_position']=simulated.get('simulated_position')
        result['rivals']['nearby']=[r for r in result['rivals']['drivers'] if r.get('simulated_position') and abs(r['simulated_position']-(order['position'] or 1))<=3]
        result['race_order']=order
        planned=branch.get('planned_stop',{})
        result['pit_forecast']=pit_forecast(branch,result,planned.get('compound'),planned.get('repair_wing',True))
        return result

    def preview(self,owner,run_id,compound,repair_wing):
        if compound not in COMPOUNDS or not isinstance(repair_wing,bool):
            raise ValueError('Choose a valid tyre compound and repair option')
        result=self.get(owner,run_id)
        return {'version':result['version'],'forecast':pit_forecast(result['branch'],result['observed'],compound,repair_wing)}

    def _view(self, db, row, record, branch):
        observed = self._current(branch, row['cursor'])
        jobs = db.execute('SELECT * FROM web_jobs WHERE run_id=? ORDER BY created_at DESC LIMIT 1', (row['run_id'],)).fetchone()
        job = dict(jobs) if jobs else None
        if job:
            job['trace'] = json.loads(job['trace'])
        return {'run_id': row['run_id'], 'label': record['label'], 'version': row['version'],
                'as_of': row['cursor'], 'observed': observed, 'branch': copy.deepcopy(branch),
                'memory': self.replay._memory(db, row), 'job': job,
                'archived':branch.get('simulation_version') != MODEL['version']}

    def get(self, owner, run_id):
        with self.replay._connect() as db:
            db.execute('BEGIN')  # Branch, memory and job status must share one read snapshot.
            return self._view(db, *self._owned(db, owner, run_id))

    def list_runs(self, owner):
        with self.replay._connect() as db:
            records = db.execute('SELECT b.run_id,b.label,b.created_at,b.state FROM web_branches b WHERE owner=? ORDER BY created_at DESC', (owner,)).fetchall()
            return [{'run_id': r['run_id'], 'label': r['label'], 'created_at': r['created_at'],
                     'lap': json.loads(r['state'])['lap'], 'finished': json.loads(r['state'])['finished']} for r in records]

    @staticmethod
    def _request(request_id, version=None):
        if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', request_id):
            raise ValueError('A request ID of 1–100 letters, digits, hyphens or underscores is required')
        if version is not None and (isinstance(version, bool) or not isinstance(version, int) or version < 0):
            raise ValueError('Expected version must be a nonnegative integer')

    def create(self, owner, lap, request_id, label=''):
        self._request(request_id)
        if not isinstance(label, str) or len(label) > 80:
            raise ValueError('Scenario name must be at most 80 characters')
        # Validate before touching the database; new backward scenarios get no memory.
        observed = self.observed(lap)
        run_id = 'web-' + request_id
        if len(run_id) > 100:
            raise ValueError('Creation request ID must be at most 96 characters')
        parameters = encoded({'operation': 'create', 'lap': lap, 'label': label})
        with self.replay._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM web_branches WHERE run_id=?', (run_id,)).fetchone()
            if old:
                if old['owner'] != owner:
                    raise KeyError('Run not found')
                receipt = db.execute('SELECT * FROM web_receipts WHERE run_id=? AND request_id=?', (run_id, request_id)).fetchone()
                if not receipt or receipt['parameters'] != parameters:
                    raise Conflict('Request ID was already used with different parameters')
                return json.loads(receipt['response'])
            branch = new_branch(observed, lap)
            if lap>1 and self.replay.timeline.get('controlled_strategy_removed'):
                # A fresh late entry must have the same unpit car/timing as a
                # run advanced from the start, without inheriting past intent.
                seeded=new_branch(self.observed(1),1)
                for crossed in range(1,lap):
                    before=self.observed(crossed)
                    advance_lap(seeded,before,resolved=self._resolve_lap(seeded,before))
                for key in ('race_gap_seconds','elapsed_delta_seconds'):
                    branch[key]=seeded[key]
            branch['rivals_sha256'] = self.rivals.digest
            branch['director_mode'] = 'llm' if self.client.configured else 'local'
            self._refresh_specialists(branch,observed)
            db.execute('INSERT INTO runs VALUES (?,?,?,?,?,?,?)',
                       (run_id, self.replay.dataset_digest, observed['as_of'], 0, 'ready', lap, 'start'))
            name = label.strip() or self.replay.timeline['race'].get('location',self.replay.timeline['race'].get('circuit','Race'))+' · lap '+str(lap)
            db.execute('INSERT INTO web_branches VALUES (?,?,?,?,?)', (run_id, owner, name, encoded(branch), now()))
            row, record, branch = self._owned(db, owner, run_id)
            result = self._view(db, row, record, branch)
            db.execute('INSERT INTO web_receipts VALUES (?,?,?,?)', (run_id, request_id, parameters, encoded(result)))
        return result

    def _mutate(self, owner, run_id, request_id, version, parameters, apply):
        self._request(request_id, version)
        if version is None:
            raise ValueError('Expected version is required')
        params = encoded(parameters)
        with self.replay._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row, record, branch = self._owned(db, owner, run_id)
            receipt = db.execute('SELECT * FROM web_receipts WHERE run_id=? AND request_id=?', (run_id, request_id)).fetchone()
            if receipt:
                if receipt['parameters'] != params:
                    raise Conflict('Request ID was already used with different parameters')
                return json.loads(receipt['response'])
            if row['version'] != version:
                raise Conflict('This run changed. Refresh before acting on the latest state.')
            if branch.get('simulation_version') != MODEL['version']:
                raise Conflict('This run belongs to the earlier simulation. Its history is saved; start a new race to use the three-agent Director.')
            if branch.get('playback',{}).get('status')=='stopped':
                raise Conflict('This race is stopped. Reset Race to start a fresh run; this history remains saved.')
            if branch.get('rivals_sha256') != self.rivals.digest:
                raise Conflict('Rival data changed. Start a new scenario to keep this run pinned to its original data.')
            previous_clock=copy.deepcopy(branch.get('playback',{}))
            previous_lap=branch['lap']
            job_id = apply(db, row, branch)
            row['version'] += 1
            self._refresh_specialists(branch,self._current(branch,row['cursor']))
            if previous_lap!=branch['lap']:
                branch['playback']['progress_fraction']=0
            if branch['playback']['status']=='running':
                wall=time.time();duration=self._lap_delay(branch)
                if previous_clock.get('status')=='running' and previous_lap==branch['lap'] and previous_clock.get('next_at'):
                    fraction=max(0,min(1,1-(previous_clock['next_at']-wall)/max(.001,previous_clock.get('lap_wall_seconds',duration))))
                else:
                    fraction=branch['playback'].get('progress_fraction',0) if previous_lap==branch['lap'] else 0
                branch['playback'].update(next_at=wall+(1-fraction)*duration,lap_wall_seconds=duration,lap_started_at=wall-fraction*duration,progress_fraction=fraction)
            db.execute('UPDATE runs SET cursor=?, version=?, status=? WHERE run_id=?',
                       (row['cursor'], row['version'], row['status'], run_id))
            db.execute('UPDATE web_branches SET state=? WHERE run_id=?', (encoded(branch), run_id))
            result = self._view(db, row, record, branch)
            db.execute('INSERT INTO web_receipts VALUES (?,?,?,?)', (run_id, request_id, params, encoded(result)))
        if job_id:
            self.executor.submit(self._work, owner, run_id, job_id)
        return result

    @staticmethod
    def _memory(db, row, kind, content):
        db.execute('INSERT INTO memory VALUES (?,?,?,?,?,?)',
                   (row['run_id'], uuid.uuid4().hex, kind, content, row['cursor'], row['version'] + 1))

    def remember(self, owner, run_id, text, request_id, version):
        if not isinstance(text, str) or not text.strip() or len(text) > 2000:
            raise ValueError('Write an instruction of 1–2000 characters')
        def apply(db, row, branch):
            self._memory(db, row, 'human_override', text.strip())
            branch['assessment'] = None  # Reassess against new intent; never approve stale advice.
        return self._mutate(owner, run_id, request_id, version, {'operation': 'remember', 'text': text}, apply)

    def jump(self, owner, run_id, lap, request_id, version, finish=False, monitor=True, refresh=False):
        if not isinstance(finish, bool):
            raise ValueError('Finish must be a boolean')
        self.replay._lap_cutoff(lap, 'start')
        if not isinstance(monitor,bool) or not isinstance(refresh,bool):
            raise ValueError('Monitor and refresh must be booleans')
        if finish and lap != self.total_laps:
            raise ValueError('Only the final lap can finish the race')
        def apply(db, row, branch):
            if branch['finished']:
                raise Conflict('This race is finished. Start another scenario to explore alternatives.')
            if lap < branch['lap']:
                raise Conflict('Open a fresh scenario to go backwards; this run keeps its later memory.')
            if monitor and branch['assessment'] and branch['assessment']['status'] == 'awaiting_human':
                raise Conflict('Resolve the pending strategy decision before advancing this run.')
            if db.execute("SELECT 1 FROM web_jobs WHERE run_id=? AND status IN ('queued','running')", (run_id,)).fetchone():
                raise Conflict('Wait for the assessment or retry after it finishes.')
            if not monitor:
                branch['playback'].update(status='paused',next_at=None)
                branch['resume_after_decision']=False
            branch['target_lap'] = lap
            branch['pause'] = None
            branch['assessment'] = None
            target = lap + (1 if finish else 0)
            while branch['lap'] <= min(target,self.total_laps):
                current = branch['lap']
                observed = self._race_context(branch,apply_conditions(self.observed(current),branch))
                row['cursor'] = observed['as_of']
                trigger = decision_trigger(branch, observed) if monitor else None
                if trigger:
                    branch['trigger_state'][trigger['key']]={'band':trigger.get('band',0),'lap':current}
                    branch['pause'] = trigger
                    branch['resume_after_decision']=branch['playback']['status']=='running'
                    branch['playback'].update(status='decision',next_at=None)
                    self._memory(db,row,'decision_point','Lap '+str(current)+': '+trigger['reason']+' Paused on the way to lap '+str(lap)+'.')
                    return self._enqueue(db,row,branch,branch['director_mode'])
                if current >= target:
                    break
                branch['monitor']={'lap':current,'action':'stay_out','reason':'No material strategy change; continue on the current plan.','as_of':observed['as_of']}
                before = branch['pit_count']
                advance_lap(branch, observed, resolved=self._resolve_lap(branch,observed))
                if branch['pit_count'] > before:
                    h = branch['history'][-1]
                    row['cursor'] = self.replay._lap_cutoff(self.total_laps if current==self.total_laps else current+1,'end' if current==self.total_laps else 'start')
                    self._memory(db,row,'pit_executed','Lap '+str(current)+': executed your authorized stop for '+branch['compound'].lower()+' tyres.'+(' Repaired '+', '.join(h['repaired'])+'.' if h['repaired'] else ''))
                if current == self.total_laps:
                    branch['lap'] = self.total_laps
                    branch['finished'] = True
                    branch['playback'].update(status='finished',next_at=None)
                    break
            row['cursor'] = self.replay._lap_cutoff(branch['lap'], 'end' if branch['finished'] else 'start')
            row['status'] = 'completed' if branch['finished'] else 'replaying'
            if refresh and not branch['finished']:
                self._memory(db,row,'navigation','Reached lap '+str(branch['lap'])+'. Refreshed all three specialist reports; your car state and approved stops carry forward.')
                return self._enqueue(db,row,branch,branch['director_mode'])
        return self._mutate(owner, run_id, request_id, version, {'operation': 'jump', 'lap': lap, 'finish': finish,'monitor':monitor,'refresh':refresh}, apply)

    def decide(self, owner, run_id, choice, compound, rationale, request_id, version, repair_wing=True):
        if not isinstance(rationale, str) or not 1 <= len(rationale.strip()) <= 1000:
            raise ValueError('Add a brief rationale for this human decision')
        if not isinstance(repair_wing,bool):
            raise ValueError('Repair wing must be a boolean')
        def apply(db, row, branch):
            assessment = branch['assessment']
            if not assessment or assessment['status'] != 'awaiting_human':
                raise Conflict('Run an assessment and use its current human decision panel first.')
            if choice not in {o['id'] for o in assessment['comparison']['options']}:
                raise ValueError('Choose an available strategy')
            if compound not in COMPOUNDS:
                raise ValueError('Choose a known tyre compound')
            selected = next(o for o in assessment['comparison']['options'] if o['id'] == choice)
            # Acknowledge each currently visible trigger, never any future condition.
            observed = self._current(branch,row['cursor'])
            if branch.get('pause'):
                branch['acknowledged'][branch['pause']['key']]=branch['lap']
            pending = branch['queued_pit']; branch['queued_pit'] = None
            for _ in range(30):
                trigger = decision_trigger(branch,observed)
                if not trigger:
                    break
                branch['acknowledged'][trigger['key']] = branch['lap']
                branch['trigger_state'][trigger['key']]={'band':trigger.get('band',0),'lap':branch['lap']}
            branch['queued_pit'] = pending
            branch['pause'] = None
            branch['planned_stop']={'compound':compound,'repair_wing':repair_wing}
            branch['queued_pit'] = ({'lap': selected['pit_lap'], 'compound': compound, 'authorized_by': 'human', 'repair_wing':repair_wing}
                                    if choice != 'hold' else None)
            branch['plan'] = ('Stay out until the next assessment.' if choice == 'hold' else
                              'Pit on lap ' + str(selected['pit_lap']) + ' for ' + compound.lower() + ' tyres.')
            assessment['status'] = 'approved'; assessment['selected'] = choice
            assessment['human_rationale'] = rationale.strip()
            self._memory(db, row, 'decision', branch['plan'] + ' Reason: ' + rationale.strip())
            if branch.get('resume_after_decision'):
                branch['playback'].update(status='running',next_at=None)
                branch['resume_after_decision']=False
        return self._mutate(owner, run_id, request_id, version,
                            {'operation': 'decide', 'choice': choice, 'compound': compound, 'rationale': rationale, 'repair_wing':repair_wing}, apply)

    def assess(self, owner, run_id, mode, request_id, version, compound=None, repair_wing=True):
        if mode not in ('local', 'llm'):
            raise ValueError('Choose local or llm mode')
        if mode == 'llm' and not self.client.configured:
            raise ValueError(self.client.provider_name + ' is not configured. Use its private key setup, then restart. Local analysis is ready now.')
        def apply(db, row, branch):
            if compound is not None:
                if compound not in COMPOUNDS or not isinstance(repair_wing,bool):raise ValueError('Invalid planned stop')
                branch['planned_stop']={'compound':compound,'repair_wing':repair_wing}
            branch['director_mode'] = mode
            return self._enqueue(db,row,branch,mode)
        return self._mutate(owner, run_id, request_id, version, {'operation':'assess','mode':mode,'compound':compound,'repair_wing':repair_wing}, apply)

    def _refresh_specialists(self,branch,observed):
        observed=self._race_context(branch,observed)
        trace=[]
        def log(role,tool,detail,status='ok',information=None):
            trace.append({'step':len(trace)+1,'role':role,'tool':tool,'detail':detail,'status':status,'information':information,'called_at':now(),'lap':branch['lap']})
        reports=[Specialist(role,observed,branch,log).run() for role in ROLE_TOOLS]
        branch['telemetry']={'snapshot_id':uuid.uuid4().hex,'read_at':now(),'lap':branch['lap'],'as_of':observed['as_of'],'reports':reports,'trace':trace}
        branch['telemetry_history']=(branch.get('telemetry_history',[])+[{'lap':branch['lap'],'read_at':branch['telemetry']['read_at'],'as_of':observed['as_of'],'tools':[e['tool'] for e in trace]}])[-12:]

    def _lap_delay(self,branch):
        observed=apply_conditions(self.observed(branch['lap']),branch)
        seconds=base_pace(observed)+penalty(branch['compound'],branch['tyre_age'],observed['condition'])+damage_penalty(branch)
        queue=branch.get('queued_pit')
        if queue and queue['lap']<=branch['lap']:
            seconds=base_pace(observed)+penalty(queue['compound'],0,observed['condition'])+stop_cost(branch,observed,queue.get('repair_wing',True))['total_loss_seconds']
            if not queue.get('repair_wing',True):
                seconds+=sum(MODEL['damage_seconds_per_lap'][i['type']] for i in active_damage(branch) if i['type']=='front_wing')
        return max(.25,seconds/branch['playback']['speed'])

    def playback(self,owner,run_id,action,speed,request_id,version):
        if action not in ('start','pause','stop','speed'):
            raise ValueError('Choose start, pause, stop or speed')
        if isinstance(speed,bool) or speed not in (1,2,3,5,10,20,40,80):
            raise ValueError('Playback speed must be 1, 2, 3, 5, 10, 20, 40 or 80')
        def apply(db,row,branch):
            if branch['finished']:raise Conflict('This race is finished. Start a new race.')
            if action=='stop':
                clock=branch['playback']
                if clock.get('next_at'):
                    clock['progress_fraction']=max(0,min(1,1-(clock['next_at']-time.time())/max(.001,clock.get('lap_wall_seconds',1))))
                branch['resume_after_decision']=False
                clock.update(status='stopped',next_at=None)
                branch['queued_pit']=None;branch['assessment']=None;branch['pause']=None
                branch['plan']='Race stopped. Reset Race for a fresh start; this run stays saved.'
                row['status']='stopped'
                db.execute("UPDATE web_jobs SET status='cancelled',error=?,updated_at=? WHERE run_id=? AND status IN ('queued','running')",
                           ('Race stopped by the director. No pending recommendation can execute.',now(),run_id))
                self._memory(db,row,'playback','Race stopped at lap '+str(branch['lap'])+'. Pending pit instructions and assessments cancelled.')
                return
            if action=='start':
                if branch['assessment'] and branch['assessment']['status']=='awaiting_human':
                    raise Conflict('Make your pending strategy call before resuming.')
                if db.execute("SELECT 1 FROM web_jobs WHERE run_id=? AND status IN ('queued','running')",(run_id,)).fetchone():
                    raise Conflict('Wait for the Director before resuming.')
            if action=='speed':
                branch['playback']['speed']=speed
            else:
                if action=='pause' and branch['playback'].get('next_at'):
                    branch['playback']['progress_fraction']=max(0,min(1,1-(branch['playback']['next_at']-time.time())/max(.001,branch['playback'].get('lap_wall_seconds',1))))
                branch['resume_after_decision']=False
                branch['playback'].update(status='running' if action=='start' else 'paused',speed=speed,next_at=None)
                self._memory(db,row,'playback','Race '+('started/resumed' if action=='start' else 'paused')+' at lap '+str(branch['lap'])+'.')
        return self._mutate(owner,run_id,request_id,version,{'operation':'playback','action':action,'speed':speed},apply)

    def tick(self,owner,run_id,at=None):
        """One due clock transition; also usable without sleeps in tests."""
        result=self.get(owner,run_id);b=result['branch'];clock=b.get('playback',{})
        if clock.get('status')!='running' or (clock.get('next_at') or 0)>(time.time() if at is None else at):return result
        return self.jump(owner,run_id,min(self.total_laps,b['lap']+1),uuid.uuid4().hex,result['version'],b['lap']==self.total_laps)

    def _ticker(self):
        while not self._stop.wait(.2):
            with self.replay._connect() as db:
                rows=db.execute('SELECT b.run_id,b.owner,b.state FROM web_branches b JOIN runs r ON b.run_id=r.run_id WHERE r.dataset_digest=?',(self.replay.dataset_digest,)).fetchall()
            for row in rows:
                if json.loads(row['state']).get('playback',{}).get('status')!='running':continue
                try:self.tick(row['owner'],row['run_id'])
                except Conflict:pass  # A human action won the version race; retry the next clock turn.
                except Exception:
                    with self.replay._connect() as db:
                        db.execute('BEGIN IMMEDIATE')
                        saved=db.execute('SELECT state FROM web_branches WHERE run_id=?',(row['run_id'],)).fetchone()
                        state=json.loads(saved['state']);state['playback'].update(status='paused',next_at=None,error='Playback paused. Review this checkpoint before resuming.')
                        db.execute('UPDATE web_branches SET state=? WHERE run_id=?',(encoded(state),row['run_id']))
                        db.execute('UPDATE runs SET version=version+1 WHERE run_id=?',(row['run_id'],))

    def configure_mode(self, owner, run_id, mode, request_id, version):
        if mode not in ('local','llm'):
            raise ValueError('Choose local or llm mode')
        if mode=='llm' and not self.client.configured:
            raise ValueError('The LLM is not configured. Local analysis is available.')
        def apply(db,row,branch):
            branch['director_mode']=mode
        return self._mutate(owner,run_id,request_id,version,{'operation':'mode','mode':mode},apply)

    def _enqueue(self, db, row, branch, mode):
        if branch['finished']:
            raise Conflict('Race is finished; export the run or start another scenario.')
        if db.execute("SELECT 1 FROM web_jobs WHERE run_id=? AND status IN ('queued','running')", (row['run_id'],)).fetchone():
            raise Conflict('An assessment is already running for this scenario.')
        if db.execute("SELECT count(*) FROM web_jobs WHERE status IN ('queued','running')").fetchone()[0] >= 4:
            raise Conflict('The analysis queue is full. Try again shortly.')
        job_id = uuid.uuid4().hex
        if branch['playback']['status']=='running':
            clock=branch['playback']
            if clock.get('next_at'):clock['progress_fraction']=max(0,min(1,1-(clock['next_at']-time.time())/max(.001,clock.get('lap_wall_seconds',1))))
            branch['resume_after_decision']=True
            branch['playback'].update(status='analysis',next_at=None)
        branch['assessment'] = None
        db.execute('INSERT INTO web_jobs (job_id,run_id,status,source_version,mode,trace,error,created_at,updated_at,source_lap,provider,model) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                   (job_id,row['run_id'],'queued',row['version']+1,mode,'[]',None,now(),now(),branch['lap'],
                    self.client.provider if mode=='llm' else None,self.client.model if mode=='llm' else None))
        return job_id

    def incident(self, owner, run_id, kind, request_id, version):
        choices = ('front_wing','puncture','rain','dry','sc','vsc','green','clear_conditions')
        if kind not in choices:
            raise ValueError('Choose a supported car incident or track condition')
        def apply(db,row,branch):
            if kind in ('front_wing','puncture'):
                if any(i['type']==kind for i in active_damage(branch)):
                    raise Conflict('This damage is already active. Authorize a repair stop to clear it.')
                branch['incidents'].append({'id':uuid.uuid4().hex,'type':kind,'lap':branch['lap'],
                                            'at':row['cursor'],'active':True,'source':'human scenario injection'})
            elif kind == 'clear_conditions':
                branch['condition_overrides'] = {}
            elif kind in ('rain','dry'):
                branch['condition_overrides']['condition'] = kind
            else:
                branch['condition_overrides']['race_control'] = {'sc':'SC','vsc':'VSC','green':'GREEN'}[kind]
            branch['pause'] = {'lap':branch['lap'],'key':'injected-'+kind,'reason':'You simulated '+kind.replace('_',' ')+'. Review the updated call.'}
            self._memory(db,row,'incident','Lap '+str(branch['lap'])+': simulated '+kind.replace('_',' ')+'.')
            return self._enqueue(db,row,branch,branch['director_mode'])
        return self._mutate(owner,run_id,request_id,version,{'operation':'incident','type':kind},apply)

    def request_review(self, owner, run_id, request_id, version):
        def apply(db, row, branch):
            assessment = branch['assessment']
            if not assessment or assessment['status'] != 'automatic':
                raise Conflict('Only a current automatic assessment can be opened for human review.')
            assessment['status'] = 'awaiting_human'
            assessment['needs_human'] = True
            assessment['reasons'].append('You requested manual review of the routine decision.')
        return self._mutate(owner, run_id, request_id, version, {'operation': 'review'}, apply)

    def _event(self, job_id, event):
        with self.replay._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT trace FROM web_jobs WHERE job_id=?', (job_id,)).fetchone()
            trace = json.loads(row['trace']); trace.append(event)
            db.execute('UPDATE web_jobs SET trace=?, updated_at=? WHERE job_id=?', (encoded(trace), now(), job_id))

    def _work(self, owner, run_id, job_id):
        try:
            with self.replay._connect() as db:
                db.execute('BEGIN IMMEDIATE')
                job = dict(db.execute('SELECT * FROM web_jobs WHERE job_id=?', (job_id,)).fetchone())
                if job['status']!='queued':return
                db.execute("UPDATE web_jobs SET status='running', updated_at=? WHERE job_id=?", (now(), job_id))
                row, _record, branch = self._owned(db, owner, run_id)
                memory = self.replay._memory(db, row)
                observed = self._current(branch, row['cursor'])
            workflow = StrategyWorkflow(observed, branch, memory, client=self.client,
                                        on_event=lambda e: self._event(job_id, e))
            assessment = workflow.run(job['mode'])
            with self.replay._connect() as db:
                db.execute('BEGIN IMMEDIATE')
                row, _record, branch = self._owned(db, owner, run_id)
                saved_job=db.execute('SELECT status FROM web_jobs WHERE job_id=?',(job_id,)).fetchone()
                if saved_job['status']=='cancelled':return
                if row['version'] != job['source_version']:
                    db.execute("UPDATE web_jobs SET status='stale', error=?, updated_at=? WHERE job_id=?",
                               ('New input changed this run. The old assessment was discarded; assess again.', now(), job_id))
                    return
                branch['assessment'] = assessment
                if branch['playback']['status'] in ('analysis','decision'):
                    branch['playback'].update(status='decision' if assessment['needs_human'] else 'running' if branch.get('resume_after_decision') else 'paused',next_at=None)
                    if branch['playback']['status']=='running':
                        branch['resume_after_decision']=False
                        duration=self._lap_delay(branch);wall=time.time();fraction=branch['playback'].get('progress_fraction',0)
                        branch['playback'].update(next_at=wall+(1-fraction)*duration,lap_wall_seconds=duration,lap_started_at=wall-fraction*duration)
                if assessment['status'] == 'automatic':
                    branch['pause']=None
                    # Only a robust hold can pass the autonomous policy.
                    if assessment['recommended'] != 'hold':
                        raise ValueError('Autonomous policy permits only routine hold decisions')
                    if not branch.get('queued_pit'):
                        branch['plan']='Stay out automatically; monitor each lap for a material strategy change.'
                    self._memory(db,row,'decision','Automatic routine stay-out.'+(' Existing human-authorized pit instruction remains queued.' if branch.get('queued_pit') else ' No new action needed.'))
                db.execute('UPDATE web_branches SET state=? WHERE run_id=?', (encoded(branch), run_id))
                db.execute('UPDATE runs SET version=version+1 WHERE run_id=?', (run_id,))
                db.execute("UPDATE web_jobs SET status='completed', updated_at=? WHERE job_id=?", (now(), job_id))
        except Exception:
            # Fail closed: the transaction above rolls back and the last complete
            # branch survives. Avoid exposing unexpected exception text or secrets.
            with self.replay._connect() as db:
                db.execute('BEGIN IMMEDIATE')
                db.execute("UPDATE web_jobs SET status='failed', error=?, updated_at=? WHERE job_id=? AND status!='cancelled'",
                           ('Assessment could not finish. Your prior state is safe; retry or use local analysis.', now(), job_id))
                row,_record,branch=self._owned(db,owner,run_id)
                if branch.get('playback',{}).get('status')=='analysis':
                    branch['playback'].update(status='paused',next_at=None,error='Director assessment failed. Retry this checkpoint before resuming.')
                    db.execute('UPDATE web_branches SET state=? WHERE run_id=?',(encoded(branch),run_id))
                    db.execute('UPDATE runs SET version=version+1 WHERE run_id=?',(run_id,))

    def export(self, owner, run_id):
        result = self.get(owner, run_id)
        with self.replay._connect() as db:
            jobs = [dict(row) for row in db.execute('SELECT * FROM web_jobs WHERE run_id=? ORDER BY created_at', (run_id,))]
        for job in jobs:
            job['trace'] = json.loads(job['trace'])
        return {'application': 'F1 Pitwall', 'exported_at': now(), 'dataset_sha256': self.replay.dataset_digest,
                'rivals_sha256':self.rivals.digest, 'assumptions': MODEL, 'run': result, 'assessment_history': jobs,
                'note': 'Educational simulation, not a validated race or finishing-position prediction.'}
