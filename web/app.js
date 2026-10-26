(() => {
  'use strict';
  const $ = id => document.getElementById(id), put = (id,v) => $(id).textContent=v;
  const show=(id,yes=true)=>$(id).hidden=!yes;
  const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const title=v=>String(v||'Unknown').toLowerCase().replace(/\b\w/g,c=>c.toUpperCase());
  const id=()=>crypto.randomUUID();
  const normalTitle=document.title;
  const store={get:k=>{try{return localStorage.getItem(k);}catch{return null;}},set:(k,v)=>{try{localStorage.setItem(k,v);}catch{}}};
  let current,meta,runs=[],busy=false,timer,assessmentKey=null,pollFailures=0;
  let activeAgent=0,lastSnapshot=null,previewData=null,previewVersion=null,previewDirty=false,previewPending=false;
  const total=()=>meta?.laps||56;
  const stopped=()=>current?.branch.playback?.status==='stopped';
  const racing=()=>current?.branch.playback?.status==='running';
  const running=()=>['queued','running'].includes(current?.job?.status);
  const frozen=()=>busy||running()||current?.archived||current?.branch.finished||stopped();
  function notice(text,error=false){show('message',!error&&!!text);show('error',error&&!!text);put(error?'error':'message',text||'');}
  async function api(path,data){let response;try{response=await fetch(path,{method:data?'POST':'GET',headers:data?{'Content-Type':'application/json'}:{},body:data?JSON.stringify(data):undefined});}catch{throw Error('Cannot reach the pit wall. Your last saved checkpoint is safe. Refresh when the server is available.');}const result=await response.json();if(!response.ok){const error=Error(result.error||'Request failed');error.status=response.status;throw error;}return result;}
  async function refreshRuns(){const data=await api('/api/bootstrap');meta=data.meta;runs=data.runs;$('run-select').innerHTML=runs.map(r=>'<option value="'+esc(r.run_id)+'">'+esc(r.label)+' · lap '+r.lap+'</option>').join('');$('mode-select').querySelector('[value="llm"]').disabled=!meta.llm_available;}
  function accept(data){current=data;store.set('pitwall.active.austin.v5',data.run_id);$('run-select').value=data.run_id;render();schedulePoll();}
  async function act(action,data={},text='',retry=true){if(busy||!current)return null;busy=true;renderControls();try{const result=await api('/api/runs/'+current.run_id+'/'+action,{...data,request_id:id(),expected_version:current.version});accept(result);if(text)notice(text);return result;}catch(error){try{accept(await api('/api/runs/'+current.run_id));}catch{}if(error.status===409&&retry&&['playback','incident','mode','remember','jump'].includes(action)){busy=false;return await act(action,data,text,false);}notice(error.message,true);return null;}finally{busy=false;renderControls();}}
  function renderControls(){if(!current)return;const a=current.branch.assessment,blocked=frozen();$('assess-button').disabled=blocked;$('mode-select').disabled=blocked;$('jump-button').disabled=blocked;$('next-button').disabled=blocked||a?.status==='awaiting_human';$('run-select').disabled=busy;$('new-button').disabled=busy;$('save-memory').disabled=busy||current.archived||current.branch.finished||stopped();document.querySelectorAll('[data-incident]').forEach(button=>{button.disabled=blocked||current.branch.incidents?.some(i=>i.active&&i.type===button.dataset.incident);});const options=a?.comparison.options||[];for(const [button,choice] of [['box-button','pit_now'],['stay-button','hold'],['delay-button','pit_in_2']])$(button).disabled=blocked||!options.some(o=>o.id===choice);$('compound-select').disabled=blocked;$('repair-wing').disabled=blocked;$('undercut-button').disabled=blocked||(!!a&&!options.some(o=>o.id==='pit_now'));$('start-race').disabled=blocked||racing()||a?.status==='awaiting_human';$('pause-race').disabled=busy||!racing();$('stop-race').disabled=busy||current.archived||current.branch.finished||stopped();$('reset-race').disabled=busy;$('review-box-call').disabled=blocked;show('start-race');show('pause-race');$('playback-speed').disabled=busy||running()||current.archived||current.branch.finished||stopped();}
  const roles=[['Rival Analyst','Watches nearby rivals’ pace, tyres and pit activity.'],['Race Conditions & Incidents','Checks weather, flags, safety cars and Leclerc’s damage.'],['Leclerc Performance','Tracks your simulated tyres, stint pace and degradation.']];
  function renderSpecialists(){
    const opened=new Set([...document.querySelectorAll('.tool-output[open]')].map(e=>e.dataset.tool));
    const checkpoint=current.branch.telemetry, reports=checkpoint?.reports||[];
    const trace=(current.job?.source_lap===current.branch.lap?current.job.trace:checkpoint?.trace)||[];
    put('agent-evidence-time','Replay lap '+current.branch.lap+' · '+new Date(current.as_of).toLocaleTimeString([],{timeZone:'UTC',hour12:false})+' UTC');
    put('agent-call-live','All 3 agents read '+(checkpoint?.trace.length||0)+' tools · '+new Date(checkpoint?.read_at||Date.now()).toLocaleTimeString());
    const facts=items=>'<dl class="agent-facts">'+items.map(([key,value])=>'<div><dt>'+esc(key)+'</dt><dd>'+esc(value??'Unavailable')+'</dd></div>').join('')+'</dl>';
    const num=(v,suffix='')=>typeof v==='number'?v.toFixed(2)+suffix:'Unavailable';
    $('specialist-cards').innerHTML=roles.map(([role,description],i)=>{
      const report=reports.find(r=>r.role===role),tools=report?.tools||{};let evidence='';
      if(i===0){const positions=tools.read_rival_positions||{},drivers=positions.drivers||[];
        evidence='<div class="rival-table-scroll"><table class="rival-table"><thead><tr><th>Sim.</th><th>Rival</th><th>Gap to LEC*</th><th class="tyre-cell">Tyres / age</th><th>Last lap</th></tr></thead><tbody>'+drivers.map(d=>'<tr><td>P'+esc(d.simulated_position??d.position??'?')+'</td><td>'+esc(d.name_acronym||d.full_name)+'</td><td>'+num(d.relative_gap_seconds,' s')+'</td><td class="tyre-cell">'+esc(title(d.compound))+' / '+esc(d.tyre_age??'?')+'</td><td>'+num(d.last_lap_seconds,' s')+'</td></tr>').join('')+'</tbody></table></div><p class="retrieved-label">'+drivers.length+' recorded rivals · gaps to simulated Leclerc: negative = ahead. Lap times and tyres are historical measurements; relative gaps reflect your decisions.</p>';
      }else if(i===1){const weather=tools.read_weather||{},control=tools.read_race_control||{},incidents=tools.inspect_car_incidents||{};evidence=facts([['Track',title(weather.condition)],['Track temperature',num(weather.track_temperature,' °C')],['Air temperature',num(weather.air_temperature,' °C')],['Humidity',num(weather.humidity,' %')],['Wind speed',num(weather.wind_speed,' m/s')],['Race control',control.state],['Weather age',num(weather.weather_age_seconds,' s')],['Car incidents',incidents.active?.map(x=>title(x.type.replaceAll('_',' '))).join(', ')||'None'],['Damage loss',num(incidents.modeled_loss_per_lap_seconds,' s/lap')]]);
      }else{const car=tools.read_simulated_telemetry||{},wear=tools.estimate_tyre_degradation||{},last=car.last_simulated_laps?.at(-1);evidence=facts([['Simulated position',car.simulated_position?'P'+car.simulated_position:'Pending timing'],['Your tyres',title(car.compound)],['Tyre age',car.tyre_age!=null?car.tyre_age+' laps':null],['Last simulated lap',num(last?.simulated_lap_seconds,' s')],['Pace baseline',num(car.historical_pace_reference,' s')],['Modeled tyre penalty',num(wear.penalty_seconds,' s/lap')],['Until tyre cliff',wear.laps_until_cliff!=null?wear.laps_until_cliff+' laps':null],['Tyre condition',wear.condition]]);}
      const outputs=Object.entries(tools).map(([tool,value])=>'<details class="tool-output" data-tool="'+esc(tool)+'"'+(opened.has(tool)?' open':'')+'><summary><code>'+esc(tool)+'</code><small>retrieved</small></summary><pre>'+esc(JSON.stringify(value,null,2))+'</pre></details>').join('');
      return '<article class="specialist-card" data-agent-panel="'+i+'"'+(i!==activeAgent?' hidden':'')+'><div class="agent-head"><span class="number">0'+(i+1)+'</span><h3>'+esc(role)+'</h3></div><p>'+esc(report?.summary||description)+'</p>'+evidence+'<div class="agent-tools">'+outputs+'</div><small>'+Object.keys(tools).length+' scoped tools called at this checkpoint</small></article>';
    }).join('');
    put('workflow-label',running()?'Director analyzing':racing()?'Updating each lap':'Checkpoint saved');

    document.querySelectorAll('[data-agent-index]').forEach(button=>{button.classList.toggle('active',Number(button.dataset.agentIndex)===activeAgent);button.setAttribute('aria-pressed',String(Number(button.dataset.agentIndex)===activeAgent));});
    const liveTrace=checkpoint?.trace||[];
    $('live-tool-calls').innerHTML=liveTrace.map(e=>'<li><span>'+esc(e.role)+'</span><button data-open-tool="'+esc(e.tool)+'">'+esc(e.tool)+'</button><small>'+esc(new Date(e.called_at||checkpoint.read_at).toLocaleTimeString())+'</small></li>').join('');
    $('agent-update-history').innerHTML=(current.branch.telemetry_history||[]).slice().reverse().map(h=>'<li>Lap '+h.lap+' · '+h.tools.length+' tools · '+esc(new Date(h.read_at).toLocaleTimeString())+' · replay '+esc(new Date(h.as_of).toLocaleTimeString([],{timeZone:'UTC'}))+'</li>').join('');
    const rival=reports.find(r=>r.role==='Rival Analyst'),conditions=reports.find(r=>r.role==='Race Conditions & Incidents'),leclerc=reports.find(r=>r.role==='Leclerc Performance');
    const weather=conditions?.tools.read_weather,car=leclerc?.tools.read_simulated_telemetry,wear=leclerc?.tools.estimate_tyre_degradation;
    put('rival-live-summary',(rival?.tools.read_rival_positions.drivers?.length||0)+' rivals · '+(current.observed.race_order?.ahead?.name_acronym||'No car')+' ahead');
    put('weather-live-summary',title(weather?.condition)+' · '+(weather?.track_temperature??'—')+' °C · '+(conditions?.tools.read_race_control.state||'Unknown'));
    put('leclerc-live-summary',title(car?.compound)+' · '+(car?.tyre_age??'—')+' laps · '+(wear?.condition||'Reading'));
    for(const key of ['rival','weather','leclerc'])put(key+'-live-time','Lap '+current.branch.lap+' · tools read '+new Date(checkpoint?.read_at||Date.now()).toLocaleTimeString());
    if(checkpoint?.snapshot_id!==lastSnapshot){lastSnapshot=checkpoint?.snapshot_id;document.querySelector('.live-agent-strip').classList.remove('refreshed');requestAnimationFrame(()=>document.querySelector('.live-agent-strip').classList.add('refreshed'));}
    $('tool-trace').innerHTML=trace.map(e=>'<li><b>'+esc(e.role)+' · '+esc(e.tool)+'</b>'+esc(e.detail)+(e.information!=null?'<details><summary>Retrieved information</summary><pre class="trace-information">'+esc(JSON.stringify(e.information,null,2))+'</pre></details>':'')+'</li>').join('');
    put('job-error',current.job?.error||'');
  }
  function renderBoxCall(){
    const b=current.branch,a=b.assessment;
    const pending=!!a&&a.status==='awaiting_human'&&a.needs_human&&['pit_now','pit_in_2'].includes(a.recommended)&&!running()&&!current.archived&&!b.finished&&!stopped();
    show('box-call-alert',pending);
    document.title=pending?'BOX CALL · Approval needed · Scuderia Pitwall':normalTitle;
    if(!pending)return;
    put('box-call-heading',a.recommended==='pit_now'?'BOX, BOX':'PIT WINDOW');
    put('box-call-message',a.recommended==='pit_now'?'Box this lap — your authorization is needed.':'Stop in two laps — your authorization is needed.');
    const source=a.source==='gemini'?'Gemini Director':a.source==='openai'?'LLM Director':a.source==='local_recovery'?'Recovery recommendation':'Local Director';
    put('box-call-detail',source+' · Lap '+b.lap+' · Review tyres, pit exit and repair options.');
  }
  function renderAssessment(){
    const b=current.branch,a=b.assessment,working=running();
    show('human-actions',!!a&&['automatic','awaiting_human'].includes(a.status)&&!working&&!current.archived&&!b.finished);
    show('decision-result',a?.status==='approved'&&!working);
    const damage=b.incidents?.some(i=>i.active&&i.type==='front_wing');show('wing-option',!!damage);
    show('pause-banner',!!b.pause);if(b.pause)put('pause-banner','Lap '+b.lap+': '+b.pause.reason);
    let headline='Ready for the race.',rationale='Press Start Race. Routine stay-out runs automatically. The live agents refresh each lap and ask for your approval when a strategy change is recommended.',status='Ready';
    if(racing()&&!a){headline='Stay out — monitoring.';rationale='No strategy change is needed. All three specialists update each lap; the Director requests action when the recommendation changes.';status='Monitoring';}
    else if(!a&&b.playback?.status==='paused'){headline='Race paused.';rationale='Resume, jump to a lap or ask the Director. Current position, pit-exit estimate and agent data are shown at this checkpoint.';status='Paused';}
    if(working){headline='Checking the strategy change…';rationale='The Director is reading rival timing, weather, Leclerc’s tyres and the calculated pit/recovery consequences.';status='Analyzing';}
    else if(a){headline={hold:'Stay out.',pit_now:'Box this lap.',pit_in_2:'Plan a stop in two laps.'}[a.recommended];rationale=a.rationale;status=a.status==='awaiting_human'?'Your decision needed':a.status==='approved'?'Call saved':'Automatic stay-out';}
    else if(!racing()&&current.job&&['failed','stale','interrupted'].includes(current.job.status)){headline='Reassess this checkpoint.';rationale=current.job.error||'Retry the Director or use local analysis.';status='Retry available';}
    if(stopped()){headline='Race stopped.';rationale='This run is saved. Reset Race to return to lap 1 with fresh tyres, no damage and a clean strategy.';status='Stopped';}
    if(b.finished){headline='Race complete.';rationale='Your simulated position, decisions, race state and memory are saved. Export the race or start another scenario.';status='Finished';}
    put('recommendation',headline);put('rationale',rationale);put('assessment-status',status);$('assessment-status').className='badge '+(a?.needs_human?'review':a?'success':'');
    put('source-note',a?.source==='local_recovery'?'Provider unavailable: local recovery calculation.':a?.source==='gemini'?'Gemini recommendation using all three specialist reports and computed pit estimates.':a?'Local calculation · no LLM used.':'Scoped tools refresh each lap. Quiet laps do not call the LLM.');
    if(a){const key=current.run_id+':'+current.job?.job_id;if(key!==assessmentKey){assessmentKey=key;previewDirty=false;previewData=null;previewVersion=null;$('compound-select').value=b.planned_stop?.compound||a.comparison.options.find(o=>o.id==='pit_now')?.compound||'MEDIUM';$('repair-wing').checked=b.planned_stop?.repair_wing??true;$('decision-rationale').value='';}
      if(a.status==='approved')put('decision-result',a.selected==='hold'?'Stay out confirmed. The current tyre set and unresolved damage carry forward.':'Pit instruction approved. The simulation executes it at the selected modeled pit entry.');
      $('strategy-options').innerHTML=a.comparison.options.map(o=>'<div><b>'+esc(o.label)+'</b> · '+esc(title(o.compound))+' · '+(o.delta_to_hold_seconds>0?'+':'')+o.delta_to_hold_seconds+' s versus staying out over '+a.comparison.horizon_laps+' laps.</div>').join('');
    }else $('strategy-options').textContent='Ask the Director to compare the options.';
    const opportunities=a?.undercut_opportunities||b.telemetry?.reports.find(r=>r.role==='Rival Analyst')?.tools.screen_undercut?.opportunities||[];
    show('undercut-opportunity',opportunities.length>0&&!b.finished);if(opportunities.length){const u=opportunities[0];put('undercut-summary',u.name+' is '+u.historical_gap_ahead_seconds+' s ahead of your simulated car on '+u.rival_tyre_age+'-lap-old tyres. Review the pit-exit and recovery estimates before approving a stop.');}
    put('plan',a?.needs_human&&!b.queued_pit?'Awaiting your call. No new stop has been authorized.':b.queued_pit?'Your plan: box on lap '+b.queued_pit.lap+' for '+title(b.queued_pit.compound)+' tyres'+(b.queued_pit.repair_wing&&damage?' and wing replacement':'')+'.':b.plan);
    put('next-button',b.finished?'Race complete':b.queued_pit&&b.queued_pit.lap<=b.lap?'Run my authorized stop →':'Advance to next action →');
    put('next-note',a?.needs_human?'Approve the change or choose Stay out.':'Quiet laps continue without a confirmation.');
    renderBoxCall();
  }
  function render(){
    if(!current)return;const b=current.branch,o=current.observed;
    const selected=$('run-select').querySelector('option[value="'+current.run_id+'"]');if(selected)selected.textContent=current.label+' · lap '+b.lap;
    show('loading',false);show('dashboard');show('archive-notice',current.archived);
    $('lap-heading').innerHTML=(b.finished?'Race finished':'Lap '+b.lap)+' <small>/ '+total()+'</small>';
    put('boundary-label',b.finished?'CHEQUERED FLAG':'CURRENT CHECKPOINT');$('progress-fill').style.width=(b.lap/total()*100)+'%';put('saved-label','Saved · '+current.memory.length+' memory records');
    if(document.activeElement!==$('lap-input'))$('lap-input').value=b.target_lap>b.lap?b.target_lap:Math.min(total(),Math.max(20,b.lap));
    put('branch-tyre',title(b.compound));put('branch-age',b.tyre_age+' laps on this set');put('pit-count',b.pit_count);
    const damage=b.incidents?.filter(i=>i.active)||[];put('car-health',damage.length?damage.map(i=>i.type==='puncture'?'Puncture':'Wing damage').join(' + '):'Healthy');$('car-health').className=damage.length?'damaged':'';put('damage-loss',damage.length?'Damage persists until repaired':'No active damage');
    put('condition',title(o.condition));put('track-temp',o.track_temperature!=null?o.track_temperature+' °C track':'Temperature unavailable');put('flag',{GREEN:'Green flag',SC:'Safety car',VSC:'Virtual safety car',RED:'Red flag'}[o.race_control]||title(o.race_control));put('override-label',Object.keys(b.condition_overrides||{}).length?'Simulated override':'Recorded race conditions');
    if(!running())$('mode-select').value=b.director_mode||'local';
    put('evidence-summary','Replay '+current.as_of+'. Historical Leclerc reference position: '+(o.historical_position?'P'+o.historical_position:'unavailable')+'. The prominent position is your simulated car. Clean historical pace reference: '+(o.pace_seconds??'unavailable')+' s.');
    $('evidence-issues').innerHTML=o.issues.map(i=>'<li>'+esc(i)+'</li>').join('');put('branch-delta','Time effect versus holding entry tyres: '+b.delta_seconds+' s. Leclerc’s race gap carries simulated pace and pit loss independently of historical stops.');
    $('memory-list').innerHTML=current.memory.length?current.memory.slice(-8).reverse().map(m=>'<li><small>'+esc(title(m.kind.replaceAll('_',' ')))+' · '+esc(new Date(m.as_of).toLocaleTimeString([],{timeZone:'UTC',hour12:false}))+' UTC</small>'+esc(m.content)+'</li>').join(''):'<li class="empty">No calls yet. Your instructions, incidents and approvals appear here.</li>';
    const clock=b.playback||{};put('race-status',b.finished?'Finished':{running:'Race running',ready:'Ready to start',paused:'Paused',decision:'Strategy change',analysis:'Director reviewing',interrupted:'Paused after restart',stopped:'Stopped'}[clock.status]||'Paused');$('race-status').className='badge '+(racing()?'active':'');
    put('start-race',clock.status==='ready'?'▶ Start Race':stopped()?'▶ Race stopped':racing()?'▶ Race running':'▶ Resume Race');put('race-clock-note',racing()?'Playback '+clock.speed+'× · routine stay-out is automatic.':clock.error||'Every pit stop needs your approval.');if(document.activeElement!==$('playback-speed'))$('playback-speed').value=clock.speed||1;
    renderAssessment();renderSpecialists();renderProjection();renderControls();
  }
  function renderProjection(forecast){
    const order=current.observed.race_order||{};
    put('current-position',order.position?'P'+order.position:'Timing pending');
    put('current-neighbor',order.ahead?(order.gap_to_ahead_seconds?.toFixed(2)+' s behind '+order.ahead.full_name):order.position===1?'Leading the simulated field':'Awaiting rival timing');
    put('position-quality',order.quality||'Simulation estimate');
    const forecastValue=forecast||(previewData&&previewVersion===current.version?previewData:current.observed.pit_forecast);
    if(!forecastValue)return;
    const f=forecastValue,c=f.stop_cost;
    put('pit-exit-label',current.branch.pit_count?'ANOTHER STOP NOW · ESTIMATED REJOIN':'IF YOU BOX NOW · ESTIMATED REJOIN');
    put('pit-exit-position',f.available&&f.pit_exit_position?'P'+f.pit_exit_position:'Pit unavailable');
    put('pit-exit-neighbor',f.rejoin_behind?f.gap_behind_seconds.toFixed(2)+' s behind '+f.rejoin_behind.full_name:f.available?'Rejoin ahead of the field':current.branch.finished?'Race complete':'Red flag: no executable stop');
    put('pit-exit-cost',c.total_loss_seconds.toFixed(2)+(current.branch.pit_count?' s additional estimated loss · ':' s estimated loss · ')+(f.positions_lost??'—')+' positions dropped');
    put('recovery-count',f.potential_overtakes+' potential '+(f.potential_overtakes===1?'catch':'catches'));
    put('recovery-summary',f.potential_overtakes?'Over '+f.horizon_laps+' laps; potential recovery to P'+f.potential_position_after_recovery:'No catch supported over the next '+f.horizon_laps+' laps at current pace.');
    put('projection-selection',title(f.compound)+(c.repair_wing?' + wing replacement':''));
    $('pit-cost-breakdown').innerHTML=[['Pit-lane transit',c.transit_seconds],['Tyre service',c.tyre_service_seconds],['Wing service (parallel with tyres)',c.wing_service_seconds],['Stationary service total',c.stationary_seconds],['Puncture checks',c.puncture_checks_seconds],['Traffic allowance',c.traffic_seconds],['Net loss',c.total_loss_seconds]].map(([label,value])=>'<div><dt>'+esc(label)+'</dt><dd>'+Number(value).toFixed(2)+' s</dd></div>').join('');
    $('flag-costs').innerHTML=Object.entries(f.cost_by_flag||{}).map(([flag,profile])=>'<span class="'+(flag===c.race_control?'selected':'')+'">'+({GREEN:'Green',VSC:'VSC',SC:'Safety car'}[flag])+': <b>'+profile.total_loss_seconds.toFixed(2)+' s</b></span>').join('');
    put('fresh-pace','Estimated pace on '+title(f.compound)+' tyres: '+f.fresh_tyre_pace_seconds.toFixed(2)+' s/lap. Catch estimates use measured rival pace plus modeled wear.');
    $('recovery-targets').innerHTML=f.potential_targets?.length?'<table class="rival-table"><thead><tr><th>Catch opportunity</th><th>Gap after pit</th><th>Pace advantage</th><th>Catch in</th></tr></thead><tbody>'+f.potential_targets.map(t=>'<tr><td>'+esc(t.name)+'</td><td>'+t.gap_after_pit_seconds+' s</td><td>'+t.initial_pace_advantage_seconds+' s/lap</td><td>~'+t.estimated_catch_in_laps+' laps</td></tr>').join('')+'</tbody></table>':'<p class="muted">'+(c.race_control==='GREEN'?'No supported recovery targets in this horizon.':'No passing forecast under '+c.race_control+'.')+'</p>';
    if(previewDirty&&previewVersion!==current.version&&!previewPending)requestPreview();
  }
  async function requestPreview(){if(!current||previewPending)return;previewPending=true;const version=current.version,run=current.run_id;
    try{const result=await api('/api/runs/'+run+'/preview?compound='+encodeURIComponent($('compound-select').value)+'&repair_wing='+$('repair-wing').checked);if(current.run_id===run&&current.version===version&&result.version===version){previewData=result.forecast;previewVersion=version;renderProjection(previewData);}}
    catch(error){notice(error.message,true);}finally{previewPending=false;}
  }
  function setAgent(index){activeAgent=index;renderSpecialists();}
  $('compound-select').addEventListener('change',()=>{previewDirty=true;previewVersion=null;requestPreview();});
  $('repair-wing').addEventListener('change',()=>{previewDirty=true;previewVersion=null;requestPreview();});
  $('live-tool-calls').addEventListener('click',event=>{const button=event.target.closest('[data-open-tool]');if(!button)return;const tool=button.dataset.openTool;const index=(current.branch.telemetry?.reports||[]).findIndex(r=>tool in r.tools);setAgent(Math.max(0,index));const output=[...document.querySelectorAll('.tool-output')].find(e=>e.dataset.tool===tool);if(output){output.open=true;output.scrollIntoView({block:'nearest'});}});
  document.querySelectorAll('[data-live-agent]').forEach(button=>button.addEventListener('click',()=>setAgent(Number(button.dataset.liveAgent))));
  setInterval(()=>{if(!current)return;const clock=current.branch.playback||{},duration=clock.lap_wall_seconds||1;const fraction=racing()&&clock.next_at?Math.max(0,Math.min(1,1-(clock.next_at-Date.now()/1000)/duration)):(clock.progress_fraction||0);put('lap-clock-text',racing()&&clock.next_at?'Lap '+current.branch.lap+' · '+Math.max(0,Math.ceil(clock.next_at-Date.now()/1000))+' real seconds to next lap':current.branch.finished?'Race complete':Math.round(fraction*100)+'% through lap · paused');$('lap-clock-fill').style.width=(fraction*100)+'%';},250);
  function schedulePoll(){clearTimeout(timer);if(!running()&&!racing())return;timer=setTimeout(async()=>{if(busy){schedulePoll();return;}try{accept(await api('/api/runs/'+current.run_id));pollFailures=0;}catch(error){notice(error.message,true);if(++pollFailures<4)schedulePoll();}},500);}
  async function advance(lap,finish=false,monitor=false,refresh=true){const result=await act('jump',{lap,finish,monitor,refresh});if(result)notice(result.branch.pause?'A strategy change is being assessed. No pit has been executed automatically.':result.branch.finished?'Race complete.': 'Reached lap '+result.branch.lap+'. All three agents refreshed their data.');}
  function openScenario(lap=1){$('new-lap').value=lap;$('new-label').value='';$('scenario-dialog').showModal();}
  async function create(lap,label=''){if(busy)return;busy=true;$('create-button').disabled=true;try{const data=await api('/api/runs',{lap,label,request_id:id()});await refreshRuns();assessmentKey=null;previewData=null;previewVersion=null;previewDirty=false;accept(data);$('scenario-dialog').close();window.scrollTo(0,0);notice('New Austin race at lap '+lap+'. Press Start Race, or jump to a checkpoint.');}catch(error){notice(error.message,true);}finally{busy=false;$('create-button').disabled=false;renderControls();}}
  async function assess(){return act('assess',{mode:$('mode-select').value,compound:$('compound-select').value,repair_wing:$('repair-wing').checked});}
  async function decide(choice){if(frozen())return;const a=current.branch.assessment;if(a?.status!=='awaiting_human'){if(a?.status!=='automatic'){await assess();return;}if(!await act('review'))return;}
    const result=await act('decision',{choice,compound:$('compound-select').value,repair_wing:$('repair-wing').checked,rationale:$('decision-rationale').value.trim()||'I chose '+({hold:'to stay out',pit_now:'to box now',pit_in_2:'a delayed stop'}[choice])+'.'});
    if(!result)return;if(choice==='pit_now'&&!racing())await advance(Math.min(total(),result.branch.lap+1),result.branch.lap===total(),true,false);else notice(racing()?'Your call is saved. The race continues.':choice==='hold'?'Stay out confirmed. Resume when ready.':'Pit instruction saved. Resume to execute it.');
  }
  $('jump-button').addEventListener('click',()=>{const lap=$('lap-input').valueAsNumber;if(!Number.isInteger(lap)||lap<1||lap>total()){notice('Choose a whole lap from 1 to '+total()+'.',true);return;}if(lap<current.branch.lap||current.branch.finished){openScenario(lap);return;}advance(lap);});
  $('lap-input').addEventListener('keydown',event=>{if(event.key==='Enter')$('jump-button').click();});
  $('next-button').addEventListener('click',()=>{const b=current.branch;advance(b.queued_pit&&b.queued_pit.lap<=b.lap?Math.min(total(),b.lap+1):b.target_lap>b.lap?b.target_lap:total(),b.lap===total(),true,false);});
  $('start-race').addEventListener('click',()=>act('playback',{action:'start',speed:Number($('playback-speed').value)},'Race running. Routine stay-out continues automatically.'));
  $('pause-race').addEventListener('click',()=>act('playback',{action:'pause',speed:Number($('playback-speed').value)},'Race paused. Resume whenever you’re ready.'));
  $('stop-race').addEventListener('click',()=>act('playback',{action:'stop',speed:Number($('playback-speed').value)},'Race stopped. Your history is saved. Reset Race for a fresh start.'));
  $('reset-race').addEventListener('click',async()=>{if(busy)return;if(!stopped()&&!current.archived&&!current.branch.finished){const stoppedRun=await act('playback',{action:'stop',speed:Number($('playback-speed').value)});if(!stoppedRun)return;}await create(1,'Leclerc · Austin 2025');});
  $('playback-speed').addEventListener('change',()=>act('playback',{action:'speed',speed:Number($('playback-speed').value)}));
  $('assess-button').addEventListener('click',assess);$('mode-select').addEventListener('change',()=>act('mode',{mode:$('mode-select').value}));
  $('undercut-button').addEventListener('click',async()=>{const a=current.branch.assessment;if(!a||a.status==='approved'){await assess();notice('Review the undercut calculation, then authorize the stop.');return;}$('decision-rationale').value='Attempt the calculated undercut with the displayed pit and recovery tradeoff.';decide('pit_now');});
  document.querySelectorAll('[data-incident]').forEach(button=>button.addEventListener('click',()=>act('incident',{type:button.dataset.incident},'Incident applied. The agents are checking the changed conditions.')));
  $('box-button').addEventListener('click',()=>decide('pit_now'));$('stay-button').addEventListener('click',()=>decide('hold'));$('delay-button').addEventListener('click',()=>decide('pit_in_2'));
  $('review-box-call').addEventListener('click',()=>{if(frozen())return;const call=$('human-actions');call.scrollIntoView({block:'center',behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'auto':'smooth'});call.focus({preventScroll:true});});
  $('new-button').addEventListener('click',()=>openScenario());$('close-scenario').addEventListener('click',()=>$('scenario-dialog').close());$('scenario-form').addEventListener('submit',event=>{event.preventDefault();create($('new-lap').valueAsNumber,$('new-label').value);});
  $('run-select').addEventListener('change',async()=>{if(busy)return;clearTimeout(timer);busy=true;try{assessmentKey=null;previewData=null;previewDirty=false;accept(await api('/api/runs/'+$('run-select').value));notice('Saved race resumed with its car state and memory.');}catch(error){notice(error.message,true);}finally{busy=false;renderControls();}});
  $('memory-form').addEventListener('submit',async event=>{event.preventDefault();if(await act('remember',{text:$('memory-input').value},'Instruction saved and available to the Director.'))$('memory-input').value='';});
  $('export-button').addEventListener('click',async()=>{try{const data=await api('/api/runs/'+current.run_id+'/export'),url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'})),link=document.createElement('a');link.href=url;link.download='pitwall-'+current.run_id+'.json';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}catch(error){notice(error.message,true);}});
  document.querySelectorAll('[data-agent-index]').forEach(button=>button.addEventListener('click',()=>setAgent(Number(button.dataset.agentIndex))));
  $('about-button').addEventListener('click',()=>$('about-dialog').showModal());$('close-about').addEventListener('click',()=>$('about-dialog').close());
  async function boot(){try{await refreshRuns();const saved=store.get('pitwall.active.austin.v5'),active=runs.find(r=>r.run_id===saved);if(active)accept(await api('/api/runs/'+active.run_id));else await create(1,'Leclerc · Austin 2025');}catch(error){show('loading',false);notice(error.message,true);}}boot();
})();
