import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE||'playwright');
const browser=await chromium.launch({headless:true,channel:'chrome'});
const output=process.env.PITWALL_QA_OUTPUT||'/tmp/pitwall-position-qa';await fs.mkdir(output,{recursive:true});
const errors=[];
try{
 const context=await browser.newContext({viewport:{width:1500,height:1050}}),page=await context.newPage();page.setDefaultTimeout(15000);
 page.on('pageerror',error=>errors.push(error.message));page.on('console',message=>{if(message.type()==='error')errors.push(message.text());});
 await page.goto(process.env.PITWALL_URL||'http://127.0.0.1:8001');await page.locator('#dashboard').waitFor({state:'visible'});
 const ready=()=>page.waitForFunction(()=>!document.querySelector('#assess-button').disabled);
 const state=async()=>await(await context.request.get('http://127.0.0.1:8001/api/runs/'+await page.locator('#run-select').inputValue())).json();
 const lap=async()=>Number((await page.locator('#lap-heading').innerText()).match(/Lap (\d+)/)?.[1]);
 const go=async target=>{await ready();await page.locator('#lap-input').fill(String(target));await page.locator('#jump-button').click();await page.waitForFunction(n=>document.querySelector('#lap-heading').textContent.includes('Lap '+n+' '),target);await ready();};
 assert.equal(await page.locator('#playback-speed').inputValue(),'1');assert.equal(await page.locator('#current-position').innerText(),'P3');assert.equal(await page.locator('.rival-table-scroll tbody tr').count(),19);assert.equal(await page.locator('#live-tool-calls li').count(),11);
 assert.ok(await page.locator('#box-call-alert').isHidden());
 await page.locator('#mode-select').selectOption('local');await ready();
 await page.locator('#playback-speed').selectOption('2');await page.locator('#start-race').click();await ready();let r=await state();assert.ok(r.branch.playback.lap_wall_seconds>45&&r.branch.playback.lap_wall_seconds<55);
 await page.waitForTimeout(700);assert.match(await page.locator('#lap-clock-text').innerText(),/real seconds/);
 await page.locator('#playback-speed').selectOption('3');await ready();r=await state();assert.ok(r.branch.playback.lap_wall_seconds>30&&r.branch.playback.lap_wall_seconds<37);
 const firstSnapshot=r.branch.telemetry.snapshot_id;await page.locator('#playback-speed').selectOption('80');await page.waitForFunction(()=>/Lap [2-9] /.test(document.querySelector('#lap-heading').textContent));
 await page.locator('#pause-race').click();await page.waitForFunction(()=>document.querySelector('#race-status').textContent==='Paused');r=await state();assert.notEqual(r.branch.telemetry.snapshot_id,firstSnapshot);const paused=await lap();await page.waitForTimeout(1600);assert.equal(await lap(),paused);
 assert.equal(r.branch.telemetry.reports.length,3);assert.ok(r.branch.telemetry.trace.every(e=>e.called_at&&e.information!=null));
 await page.locator('[data-agent-index="1"]').click();assert.match(await page.locator('[data-agent-panel="1"]').innerText(),/Air temperature/);
 await page.locator('[data-agent-index="2"]').click();assert.match(await page.locator('[data-agent-panel="2"]').innerText(),/evaluate_pit_consequences/);
 await page.locator('[data-agent-index="0"]').click();await go(15);r=await state();
 assert.equal(await page.locator('#current-position').innerText(),'P2');assert.ok(Math.abs(r.branch.race_gap_seconds-5.054)<.01);assert.equal(r.branch.pit_count,0);
 assert.ok(await page.locator('#box-call-alert').isVisible());assert.match(await page.title(),/BOX CALL/);
 assert.equal(await page.locator('.box-call-light').evaluate(e=>getComputedStyle(e).animationName),'boxCallPulse');
 await page.reload();await page.locator('#dashboard').waitFor({state:'visible'});await ready();assert.ok(await page.locator('#box-call-alert').isVisible());
 await page.locator('#review-box-call').click();assert.equal(await page.evaluate(()=>document.activeElement.id),'human-actions');assert.equal((await state()).branch.pit_count,0);
 await page.emulateMedia({reducedMotion:'reduce'});assert.equal(await page.locator('.box-call-light').evaluate(e=>getComputedStyle(e).animationName),'none');
 await page.setViewportSize({width:390,height:900});await page.evaluate(()=>window.scrollTo(0,document.body.scrollHeight));
 const alertBounds=await page.locator('#box-call-alert').boundingBox();assert.ok(alertBounds.x>=0&&alertBounds.y>=0&&alertBounds.x+alertBounds.width<=390&&alertBounds.y+alertBounds.height<=900);
 await page.screenshot({path:path.join(output,'box-call-mobile.png')});await page.setViewportSize({width:1500,height:1050});await page.emulateMedia({reducedMotion:'no-preference'});
 await page.evaluate(()=>window.scrollTo(0,0));await page.screenshot({path:path.join(output,'box-call-desktop.png')});
 await page.locator('#stay-button').click();await ready();assert.ok(await page.locator('#box-call-alert').isHidden());assert.doesNotMatch(await page.title(),/BOX CALL/);
 await go(20);r=await state();
 assert.equal(await lap(),20);assert.equal(r.branch.pit_count,0);assert.equal(await page.locator('#current-position').innerText(),'P'+r.observed.race_order.position);assert.equal(await page.locator('#pit-exit-position').innerText(),'P'+r.observed.pit_forecast.pit_exit_position);
 assert.match(await page.locator('#pit-exit-neighbor').innerText(),/s behind/);assert.match(await page.locator('#recovery-count').innerText(),/potential/);
 await page.locator('#wing-button').click();await page.waitForFunction(()=>document.querySelector('#car-health').textContent.includes('Wing damage'));await ready();assert.equal(await page.locator('#pit-count').innerText(),'0');
 const wingCost=(await state()).observed.pit_forecast.stop_cost.total_loss_seconds;
 await page.locator('#repair-wing').uncheck();await page.waitForFunction(cost=>document.querySelector('#pit-exit-cost').textContent.includes((cost-5).toFixed(2)+' s'),wingCost);
 await page.locator('#repair-wing').check();await page.waitForFunction(cost=>document.querySelector('#pit-exit-cost').textContent.includes(cost.toFixed(2)+' s'),wingCost);
 await page.locator('#puncture-button').click();await page.waitForFunction(()=>document.querySelector('#car-health').textContent.includes('Puncture'));await ready();r=await state();assert.equal(r.branch.pit_count,0);assert.equal(r.observed.pit_forecast.stop_cost.puncture_checks_seconds,1.5);
 await page.evaluate(()=>window.scrollTo(0,0));await page.screenshot({path:path.join(output,'pit-position-damage.png'),fullPage:true});
 await page.locator('#box-button').click();await page.waitForFunction(()=>document.querySelector('#pit-count').textContent==='1');await ready();assert.equal(await page.locator('#car-health').innerText(),'Healthy');r=await state();assert.equal(r.branch.history.filter(h=>h.pit).length,1);assert.equal(r.branch.history.find(h=>h.pit).stop_cost.puncture_checks_seconds,1.5);
 assert.ok(await page.locator('#box-call-alert').isHidden());
 // Fresh tyres under a safety car should continue on an automatic hold.
 if((await state()).branch.assessment?.status==='awaiting_human'){await page.locator('#stay-button').click();await ready();}
 await page.locator('.incident-panel > details > summary').click();await page.locator('#start-race').click();await page.locator('[data-incident="sc"]').click();await ready();r=await state();assert.equal(r.branch.assessment?.recommended,'hold');assert.equal(r.branch.assessment?.needs_human,false);assert.equal(r.branch.playback.status,'running');
 const scLap=r.branch.lap;await page.waitForFunction(n=>Number(document.querySelector('#lap-heading').textContent.match(/Lap (\d+)/)[1])>=n+2,scLap);assert.equal(await page.locator('#race-status').innerText(),'Race running');
 await page.locator('#pause-race').click();await ready();await page.locator('[data-incident="vsc"]').click();await ready();r=await state();const costs=r.observed.pit_forecast.cost_by_flag;assert.ok(costs.SC.total_loss_seconds<costs.VSC.total_loss_seconds&&costs.VSC.total_loss_seconds<costs.GREEN.total_loss_seconds);assert.equal(r.observed.pit_forecast.potential_overtakes,0);
 await page.reload();await page.locator('#dashboard').waitFor({state:'visible'});assert.equal(await page.locator('#pit-count').innerText(),'1');assert.equal(await page.locator('#flag').innerText(),'Virtual safety car');
 const downloadPromise=page.waitForEvent('download');await page.locator('#export-button').click();const artifact=await(await downloadPromise).path(),exported=JSON.parse(await fs.readFile(artifact,'utf8'));assert.ok(exported.run.branch.race_gap_seconds!=null);assert.ok(exported.run.branch.telemetry_history.length>1);
 const sizes=[];for(const width of [1500,1200,1024,768,390,320]){await page.setViewportSize({width,height:1000});const size=await page.evaluate(()=>({width:innerWidth,scroll:document.documentElement.scrollWidth}));assert.ok(size.scroll<=width,'Horizontal overflow at '+width+': '+size.scroll);sizes.push(size);}
 await page.screenshot({path:path.join(output,'mobile.png'),fullPage:true});assert.deepEqual(errors,[]);
 // Reproduce the reported early stop in a separate, fresh race.
 await page.setViewportSize({width:1500,height:1050});await page.locator('#new-button').click();await page.locator('#new-lap').fill('1');await page.locator('#create-button').click();await page.locator('#scenario-dialog').waitFor({state:'hidden'});await page.waitForFunction(()=>document.querySelector('#lap-heading').textContent.includes('Lap 1 '));await ready();
 await page.locator('#mode-select').selectOption('local');await ready();await go(15);await page.locator('#compound-select').selectOption('MEDIUM');await page.locator('#box-button').click();
 await page.waitForFunction(()=>document.querySelector('#pit-count').textContent==='1');await ready();r=await state();
 assert.equal(r.branch.lap,16);assert.equal(await page.locator('#current-position').innerText(),'P11');assert.ok(Math.abs(r.branch.race_gap_seconds-28.955)<.01);
 assert.match(await page.locator('#pit-exit-label').innerText(),/ANOTHER STOP NOW/);
 await page.screenshot({path:path.join(output,'lap-15-human-stop.png'),fullPage:true});
 // Pause must remain visible and readable; Stop is terminal, Reset is isolated.
 assert.equal(await page.locator('#agent-tests-button').count(),0);
 const oldId=r.run_id;await page.locator('#reset-race').click();
 await page.waitForFunction(old=>document.querySelector('#run-select').value!==old,oldId);await ready();r=await state();
 assert.equal(r.branch.lap,1);assert.equal(r.branch.pit_count,0);assert.equal(r.branch.history.length,0);assert.equal(r.memory.length,0);assert.deepEqual(r.branch.incidents,[]);
 const savedOld=await(await context.request.get('http://127.0.0.1:8001/api/runs/'+oldId)).json();assert.equal(savedOld.branch.playback.status,'stopped');assert.equal(savedOld.branch.pit_count,1);
 await page.screenshot({path:path.join(output,'ferrari-ready.png'),fullPage:true});
 await page.locator('#start-race').click();await page.waitForFunction(()=>document.querySelector('#race-status').textContent==='Race running');
 for(const button of ['#pause-race','#stop-race','#reset-race'])assert.ok(await page.locator(button).isVisible());
 const contrast=await page.locator('#pause-race').evaluate(button=>{const style=getComputedStyle(button);const luminance=color=>{const rgb=color.match(/[\d.]+/g).slice(0,3).map(n=>Number(n)/255).map(v=>v<=.04045?v/12.92:((v+.055)/1.055)**2.4);return rgb[0]*.2126+rgb[1]*.7152+rgb[2]*.0722;};const a=luminance(style.color),b=luminance(style.backgroundColor);return (Math.max(a,b)+.05)/(Math.min(a,b)+.05);});assert.ok(contrast>=4.5,'Pause contrast '+contrast);
 await page.screenshot({path:path.join(output,'ferrari-running.png'),fullPage:true});
 await page.locator('#pause-race').click();await page.waitForFunction(()=>document.querySelector('#race-status').textContent==='Paused');
 await page.locator('#start-race').click();await page.waitForFunction(()=>document.querySelector('#race-status').textContent==='Race running');
 await page.locator('#stop-race').click();await page.waitForFunction(()=>document.querySelector('#race-status').textContent==='Stopped');assert.ok(await page.locator('#start-race').isDisabled());
 await page.reload();await page.locator('#dashboard').waitFor({state:'visible'});assert.equal(await page.locator('#race-status').innerText(),'Stopped');
 await page.locator('#reset-race').click();await page.waitForFunction(()=>document.querySelector('#race-status').textContent==='Ready to start');await ready();
 for(const width of [1500,1024,768,390,320]){await page.setViewportSize({width,height:1000});assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'Control layout at '+width);for(const button of ['#pause-race','#stop-race','#reset-race'])assert.ok(await page.locator(button).isVisible());}
 await page.screenshot({path:path.join(output,'ferrari-mobile.png'),fullPage:true});assert.deepEqual(errors,[]);
 console.log(JSON.stringify({status:'passed',checks:['persistent pit-call beacon','review shortcut does not authorize','clears on stay out and approval','pending alert survives refresh','reduced-motion solid beacon','mobile alert stays in viewport','lap 15 baseline P2 and recorded gap','lap 15 human stop to P11 without artificial drift','simulated position','pit exit rank and neighbor','recovery estimates','1x 2x 3x timings','mid-lap speed change','all 19 rivals','11 timestamped tool calls','weather and car tabs','lap data refresh','repair preview','puncture checks','authorized pit','automatic SC stay-out without repeated pauses','distinct SC/VSC costs','no SC overtakes','reload','export','responsive widths'],sizes,screenshots:output,consoleErrors:errors}));
}finally{await browser.close();}
